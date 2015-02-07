# Functions to read and access the broctl configuration.

import os
import socket
import re
import json

from BroControl import py3bro
from BroControl import node as node_mod
from BroControl import options
from .state import SqliteState
from .version import VERSION

from BroControl import graph


# Class storing the broctl configuration.
#
# This class provides access to four types of configuration/state:
#
# - the global broctl configuration from broctl.cfg
# - the node configuration from node.cfg
# - dynamic state variables which are kept across restarts in spool/broctl.dat

Config = None # Globally accessible instance of Configuration.

class ConfigurationError(Exception):
    pass

class Configuration:
    def __init__(self, basedir, ui, localaddrs=[], state=None):
        from BroControl import execute

        config_file = os.path.join(basedir, "etc/broctl.cfg")
        broscriptdir = os.path.join(basedir, "share/bro")
        self.ui = ui
        self.localaddrs = localaddrs
        global Config
        Config = self

        self.config = {}
        self.state = {}
        self.nodelist = {}

        # Read broctl.cfg.
        self.config = self._readConfig(config_file)
        if self.config is None:
            raise RuntimeError

        # Set defaults for options we get passed in.
        self._setOption("brobase", basedir)
        self._setOption("broscriptdir", broscriptdir)
        self._setOption("version", VERSION)

        # Initialize options.
        for opt in options.options:
            if not opt.dontinit:
                self._setOption(opt.name, opt.default)

        if state:
            self.state_store = state
        else:
            self.state_store = SqliteState(self.statefile)

        # Set defaults for options we derive dynamically.
        self._setOption("mailto", "%s" % os.getenv("USER"))
        self._setOption("mailfrom", "Big Brother <bro@%s>" % socket.gethostname())
        self._setOption("mailalarmsto", self.config["mailto"])

        # Determine operating system.
        (success, output) = execute.runLocalCmd("uname")
        if not success:
            raise RuntimeError("cannot run uname")
        self._setOption("os", output[0].lower().strip())

        if self.config["os"] == "linux":
            self._setOption("pin_command", "taskset -c")
        elif self.config["os"] == "freebsd":
            self._setOption("pin_command", "cpuset -l")
        else:
            self._setOption("pin_command", "")

        # Find the time command (should be a GNU time for best results).
        (success, output) = execute.runLocalCmd("which time")
        if success:
            self._setOption("time", output[0].lower().strip())
        else:
            self._setOption("time", "")

        self.overlay = graph.BGraph()

    def initPostPlugins(self):
        self.readState()

        # Read node.cfg
        self.nodelist, self.peers = self._readNodes()
        if not self.nodelist:
            return False

        # If "env_vars" was specified in broctl.cfg, then apply to all nodes.
        varlist = self.config.get("env_vars")
        if varlist:
            try:
                global_env_vars = self._getEnvVarDict(varlist)
            except ValueError as err:
                raise ConfigurationError("env_vars option in broctl.cfg: %s" % err)

            for node in self.nodes("all"):
                for (key, val) in global_env_vars.items():
                    # Values from node.cfg take precedence over broctl.cfg
                    node.env_vars.setdefault(key, val)

        # Now that the nodes have been read in, set the standalone config option.
        standalone = "0"
        for node in self.nodes("all"):
            if node.type == "standalone":
                standalone = "1"

        self._setOption("standalone", standalone)

        # Make sure cron flag is cleared.
        self.config["cron"] = "0"

        return True

    # Provides access to the configuration options via the dereference operator.
    # Lookups the attribute in broctl.cfg first, then in the dynamic variables
    # from broctl.dat.
    def __getattr__(self, attr):
        if attr in self.config:
            return self.config[attr]
        if attr in self.state:
            return self.state[attr]
        raise AttributeError(attr)

    # Returns True if attribute is defined.
    def hasAttr(self, attr):
        if attr in self.config:
            return True
        if attr in self.state:
            return True
        return False

    # Returns a sorted list of all broctl.cfg entries.
    # Includes dynamic variables if dynamic is true.
    def options(self, dynamic=True):
        optlist = list(self.config.items())
        if dynamic:
            optlist += list(self.state.items())

        optlist.sort()
        return optlist

    # Returns a list of Nodes (the list will be empty if no matching nodes
    # are found).  The returned list is sorted by node type, and by node name
    # for each type.
    # - If tag is None, all Nodes are returned.
    # - If tag is "all", all Nodes are returned if "expand_all" is true.
    #     If "expand_all" is false, returns an empty list in this case.
    # - If tag is "proxies", all proxy Nodes are returned.
    # - If tag is "workers", all worker Nodes are returned.
    # - If tag is "manager", the manager Node is returned (cluster config) or
    #     the standalone Node is returned (standalone config).
    # - If tag is "standalone", the standalone Node is returned.
    # - If tag is the name of a node, then that node is returned.
    def nodes(self, tag=None, expand_all=True):
        nodes = []
        type = None

        if tag == "all":
            if not expand_all:
                return []

            tag = None

        elif tag == "standalone":
            type = "standalone"

        elif tag == "manager":
            type = "manager"

        elif tag == "proxies":
            type = "proxy"

        elif tag == "workers":
            type = "worker"

        for n in self.nodelist.values():
            if type:
                if type == n.type:
                    nodes += [n]

            elif tag == n.name or not tag:
                nodes += [n]

        nodes.sort(key=lambda n: (n.type, n.name))

        if not nodes and tag == "manager":
            nodes = self.nodes("standalone")

        return nodes

    def manager(self):
        n = self.nodes("manager")
        if n:
            return n[0]
        n = self.nodes("standalone")
        if n:
            return n[0]
        return None

    # Returns a list of nodes which is a subset of the result a similar call to
    # nodes() would yield but within which each host appears only once.
    # If "nolocal" parameter is True, then exclude the local host from results.
    def hosts(self, tag = None, nolocal=False):
        hosts = {}
        nodelist = []
        for node in self.nodes(tag):
            if node.host in hosts:
                continue

            if (not nolocal) or (nolocal and node.addr not in self.localaddrs):
                hosts[node.host] = 1
                nodelist.append(node)

        return nodelist

    # Replace all occurences of "${option}", with option being either
    # broctl.cfg option or a dynamic variable, with the corresponding value.
    # Defaults to replacement with the empty string for unknown options.
    def subst(self, str):
        while True:
            m = re.search(r"(\$\{([A-Za-z]+)(:([^}]+))?\})", str)
            if not m:
                return str

            key = m.group(2).lower()
            if self.hasAttr(key):
                value = self.__getattr__(key)
            else:
                value = m.group(4)

            if not value:
                value = ""

            str = str[0:m.start(1)] + value + str[m.end(1):]


    # Convert string into list of integers (ValueError is raised if any
    # item in the list is not a non-negative integer).
    def _getPinCPUList(self, str, numprocs):
        if not str:
            return []

        cpulist = [ int(x) for x in str.split(",") ]
        # Minimum allowed CPU number is zero.
        if min(cpulist) < 0:
            raise ValueError

        # Make sure list is at least as long as number of worker processes.
        cpulen = len(cpulist)
        if numprocs > cpulen:
            cpulist = [ cpulist[i % cpulen] for i in range(numprocs) ]

        return cpulist

    # Convert a string consisting of a comma-separated list of environment
    # variables (e.g. "VAR1=123, VAR2=456") to a dictionary.
    # If the string is empty, then return an empty dictionary.  Upon error,
    # a ValueError is raised.
    def _getEnvVarDict(self, str):
        env_vars = {}

        if str:
            # If the entire string is quoted, then remove only those quotes.
            if (str.startswith('"') and str.endswith('"')) or (str.startswith("'") and str.endswith("'")):
                str = str[1:-1]

        if str:
            for keyval in str.split(","):
                try:
                    (key, val) = keyval.split("=", 1)
                except ValueError:
                    raise ConfigurationError("missing '=' after environment variable name")

                if not key.strip():
                    raise ConfigurationError("missing environment variable name")

                env_vars[key.strip()] = val.strip()

        return env_vars

    # Parse node.cfg.
    # Old version of readNodes, can be deleted
    # functionality has been split onto
    # - readNodes,
    # - checkNodes, and
    # - checkNodeStore
    def _readNodes_Obsolete(self):
        config = py3bro.configparser.SafeConfigParser()
        if not config.read(self.nodecfg):
            raise ConfigurationError("cannot read '%s'" % self.nodecfg)

        manager = False
        proxy = False
        worker = False
        standalone = False

        file = self.nodecfg
        nodestore = {}

        counts = {}

        for sec in config.sections():
            node = node_mod.Node(self, sec)
            nodestore[sec] = node

            for (key, val) in config.items(sec):

                key = key.replace(".", "_")

                if key not in node_mod.Node._keys:
                    self.ui.warn("%s: unknown key '%s' in section '%s'" % (file, key, sec))
                    continue

                if key == "type":
                    if val == "manager":
                        if manager:
                            raise ConfigurationError("only one manager can be defined")
                        manager = True

                    elif val == "proxy":
                        proxy = True

                    elif val == "worker":
                        worker = True

                    elif val == "standalone":
                        standalone = True

                    else:
                        raise ConfigurationError("%s: unknown type '%s' in section '%s'" % (file, val, sec))


                node.__dict__[key] = val

            # Convert env_vars from a string to a dictionary.
            try:
                node.env_vars = self._getEnvVarDict(node.env_vars)
            except ValueError as err:
                raise ConfigurationError("%s: section %s: %s" % (file, sec, err))

            try:
                addrinfo = socket.getaddrinfo(node.host, None, 0, 0, socket.SOL_TCP)
                if len(addrinfo) == 0:
                    raise ConfigurationError("%s: no addresses resolved in section '%s' for host %s" % (file, sec, node.host))

                addr_str = addrinfo[0][4][0]
                # zone_id is handled manually, so strip it if it's there
                node.addr = addr_str.split('%')[0]
            except AttributeError:
                raise ConfigurationError("%s: no host given in section '%s'" % (file, sec))
            except socket.gaierror as e:
                raise ConfigurationError("%s: unknown host '%s' in section '%s' [%s]" % (file, node.host, sec, e.args[1]))

            # Each node gets a number unique across its type.
            type = nodestore[sec].type
            try:
                counts[type] += 1
            except KeyError:
                counts[type] = 1

            node.count = counts[type]

            numprocs = 0

            if node.lb_procs:
                try:
                    numprocs = int(node.lb_procs)
                    if numprocs < 1:
                        raise ConfigurationError("%s: value of lb_procs must be at least 1 in section '%s'" % (file, sec))
                except ValueError:
                    raise ConfigurationError("%s: value of lb_procs must be an integer in section '%s'" % (file, sec))
            elif node.lb_method:
                raise ConfigurationError("%s: load balancing requires lb_procs in section '%s'" % (file, sec))

            try:
                pin_cpus = self._getPinCPUList(node.pin_cpus, numprocs)
            except ValueError:
                raise ConfigurationError("%s: pin_cpus must be list of non-negative integers in section '%s'" % (file, sec))

            if pin_cpus:
                node.pin_cpus = pin_cpus[0]

            if node.lb_procs:
                if not node.lb_method:
                    raise ConfigurationError("%s: no load balancing method given in section '%s'" % (file, sec))

                if node.lb_method not in ("pf_ring", "myricom", "interfaces"):
                    raise ConfigurationError("%s: unknown load balancing method given in section '%s'" % (file, sec))

                if node.lb_method == "interfaces":
                    if not node.lb_interfaces:
                        raise ConfigurationError("%s: no list of interfaces given in section '%s'" % (file, sec))

                    # get list of interfaces to use, and assign one to each node
                    netifs = node.lb_interfaces.split(",")

                    if len(netifs) != int(node.lb_procs):
                        raise ConfigurationError("%s: number of interfaces does not match value of lb_procs in section '%s'" % (file, sec))

                    node.interface = netifs.pop().strip()

                # node names will have a numerical suffix
                node.name = "%s-1" % sec

                for num in range(2, numprocs + 1):
                    newnode = node.copy()
                    # only the node name, count, and pin_cpus need to be changed
                    newname = "%s-%d" % (sec, num)
                    newnode.name = newname
                    nodestore[newname] = newnode
                    counts[type] += 1
                    newnode.count = counts[type]
                    if pin_cpus:
                        newnode.pin_cpus = pin_cpus[num-1]

                    if newnode.lb_method == "interfaces":
                        newnode.interface = netifs.pop().strip()

        if nodestore:

            if not standalone:
                if not manager:
                    raise ConfigurationError("%s: no manager defined" % file)

                if not proxy:
                    raise ConfigurationError("%s: no proxy defined" % file)

            else:
                if len(nodestore) > 1:
                    raise ConfigurationError("%s: more than one node defined in stand-alone setup" % file)

        manageronlocalhost = False

        for n in nodestore.values():
            if not n.name:
                raise ConfigurationError("node configured without a name")

            if not n.host:
                raise ConfigurationError("no host given for node %s" % n.name)

            if not n.type:
                raise ConfigurationError("no type given for node %s" % n.name)

            if n.type == "manager":
                if n.addr not in self.localaddrs:
                    raise ConfigurationError("script must be run on manager node")

                if ( n.addr == "127.0.0.1" or n.addr == "::1" ) and n.type != "standalone":
                    manageronlocalhost = True

        # If manager is on localhost, then all other nodes must be on localhost
        if manageronlocalhost:
            for n in nodestore.values():
                if n.type != "manager" and n.type != "standalone":
                    if n.addr != "127.0.0.1" and n.addr != "::1":
                        raise ConfigurationError("cannot use localhost/127.0.0.1/::1 for manager host in nodes configuration")

        return nodestore

    # Parse node.cfg.
    def _readNodes(self):
        config = py3bro.configparser.SafeConfigParser()
        try:
            if not config.read(self.nodecfg):
                raise ConfigurationError("cannot read '%s'" % self.nodecfg)
        except py3bro.configparser.MissingSectionHeaderError:
            return self._readNodesJson()

        manager = False

        file = self.nodecfg
        nodestore = {}
        cluster = {}

        counts = {}
        for sec in config.sections():
            node = node_mod.Node(self, sec)
            nodestore[sec] = node
            node.__dict__["id"] = sec

            for (key, val) in config.items(sec):

                key = key.replace(".", "_")

                if key not in node_mod.Node._keys:
                    self.ui.warn("%s: unknown key '%s' in section '%s'" % (file, key, sec))
                    continue

                if key == "type":
                    if val == "manager":
                        if manager:
                            raise ConfigurationError("only one manager can be defined")
                        manager = True
                    elif (val != "proxy" and val != "worker" and val != "standalone"):
                        raise ConfigurationError("%s: unknown type '%s' in section '%s'" % (file, val, sec))

                node.__dict__[key] = val

            # Complete node entry, nodestore, and counts
            node, nodestore, counts = self._checkNode(node, nodestore, counts)

        # Check if nodestore is valid
        self._checkNodeStore(nodestore)
        return nodestore, cluster

    # Check and complete node entry, nodestore, and counts
    def _checkNode(self, node, nodestore, counts):

        # 1. part of _readNodes
        #
        # Convert env_vars from a string to a dictionary.
        try:
            node.env_vars = self._getEnvVarDict(node.env_vars)
        except ValueError as err:
            raise ConfigurationError("%s: section %s: %s" % (file, node.name, err))

        try:
            addrinfo = socket.getaddrinfo(node.host, None, 0, 0, socket.SOL_TCP)
            if len(addrinfo) == 0:
                raise ConfigurationError("%s: no addresses resolved in section '%s' for host %s" % (file, node.name, node.host))

            addr_str = addrinfo[0][4][0]
            # zone_id is handled manually, so strip it if it's there
            node.addr = addr_str.split('%')[0]
        except AttributeError:
            raise ConfigurationError("%s: no host given in section '%s'" % (file, node.name))
        except socket.gaierror as e:
            raise ConfigurationError("%s: unknown host '%s' in section '%s' [%s]" % (file, node.host, node.name, e.args[1]))

        # Each node gets a number unique across its type.
        type = nodestore[node.name].type
        try:
            counts[type] += 1
        except KeyError:
            counts[type] = 1

        node.count = counts[type]

        # 2. Part of _readNodes
        numprocs = 0
        if node.lb_procs:
            try:
                numprocs = int(node.lb_procs)
                if numprocs < 1:
                    raise ConfigurationError("%s: value of lb_procs must be at least 1 in section '%s'" % (file, node.name))
            except ValueError:
                raise ConfigurationError("%s: value of lb_procs must be an integer in section '%s'" % (file, node.name))
        elif node.lb_method:
            raise ConfigurationError("%s: load balancing requires lb_procs in section '%s'" % (file, node.name))

        try:
            pin_cpus = self._getPinCPUList(node.pin_cpus, numprocs)
        except ValueError:
            raise ConfigurationError("%s: pin_cpus must be list of non-negative integers in section '%s'" % (file, node.name))

        if pin_cpus:
            node.pin_cpus = pin_cpus[0]

        if node.lb_procs:
            if not node.lb_method:
                raise ConfigurationError("%s: no load balancing method given in section '%s'" % (file, node.name))

            if node.lb_method not in ("pf_ring", "myricom", "interfaces"):
                raise ConfigurationError("%s: unknown load balancing method given in section '%s'" % (file, node.name))

            if node.lb_method == "interfaces":
                if not node.lb_interfaces:
                    raise ConfigurationError("%s: no list of interfaces given in section '%s'" % (file, node.name))

                # get list of interfaces to use, and assign one to each node
                netifs = node.lb_interfaces.split(",")

                if len(netifs) != int(node.lb_procs):
                    raise ConfigurationError("%s: number of interfaces does not match value of lb_procs in section '%s'" % (file, node.name))

                node.interface = netifs.pop().strip()

            nodeName = node.name
            # node names will have a numerical suffix
            node.name = "%s-1" % node.name

            for num in range(2, numprocs + 1):
                newnode = node.copy()
                # only the node name, count, and pin_cpus need to be changed
                newname = "%s-%d" % (nodeName, num)
                newnode.name = newname
                nodestore[newname] = newnode
                counts[type] += 1
                newnode.count = counts[type]
                if pin_cpus:
                    newnode.pin_cpus = pin_cpus[num-1]

                if newnode.lb_method == "interfaces":
                    newnode.interface = netifs.pop().strip()

        nodestore[node.name] = node
        return node, nodestore, counts

    def _checkNodeStore(self, nodestore):
        if not nodestore:
            raise ConfigurationError("No nodes found")

        manageronlocalhost = False
        standalone = False
        manager = False
        proxy = False

        for n in nodestore.values():
            if not n.name:
                raise ConfigurationError("node configured without a name")

            if not n.host:
                raise ConfigurationError("no host given for node %s" % n.name)

            if not n.type:
                raise ConfigurationError("no type given for node %s" % n.name)

            if n.type == "manager":
                if n.addr not in self.localaddrs:
                    raise ConfigurationError("script must be run on manager node")

                if ( n.addr == "127.0.0.1" or n.addr == "::1" ) and n.type != "standalone":
                    manageronlocalhost = True

                manager = True

            elif n.type == "proxy":
                proxy = True

            elif n.type == "standalone":
                standalone = True

        if not standalone:
            if not manager:
                raise ConfigurationError("%s: no manager defined" % file)
            elif not proxy:
                raise ConfigurationError("%s: no proxy defined" % file)
        else:
            if len(nodestore) > 1:
                raise ConfigurationError("%s: more than one node defined in stand-alone setup" % file)

        # If manager is on localhost, then all other nodes must be on localhost
        if manageronlocalhost:
            for n in nodestore.values():
                if n.type != "manager" and n.type != "standalone":
                    if n.addr != "127.0.0.1" and n.addr != "::1":
                        raise ConfigurationError("cannot use localhost/127.0.0.1/::1 for manager host in nodes configuration")

    # Parse node.cfg in Json-Format
    def _readNodesJson(self):
        file = self.nodecfg
        if not os.path.exists(file):
            raise ConfigurationError("cannot read '%s'" % self.nodecfg)

        nodestore = {}
        counts = {}

        with open(file, 'r') as f:
            data = json.load(f)
            if "nodes" not in data.keys() or "connections" not in data.keys() or "head" not in data.keys():
                raise ConfigurationError("Misconfigured configuration file")

            head = data["head"]
            cluster_head = ""
            scope = ""

            # Iterate over all node entries
            for entry in data["nodes"]:
                if "id" not in entry.keys() or "type" not in entry.keys():
                    raise ConfigurationError("Misconfigured configuration file")

                node = ""
                # When node is a cluster, its members need to be queried
                if entry["type"] == "cluster":
                    clusterId = entry["id"]
                    # Add cluster to the graph
                    self.overlay.addNode(clusterId)

                    for val in entry["members"]:
                        if "id" not in val.keys() or "type" not in val.keys():
                            raise ConfigurationError("Misconfigured configuration file")

                        nodeId = clusterId + "::" + val["id"]
                        node = node_mod.Node(self, nodeId)
                        node.__dict__["cluster"] = clusterId

                        nodestore[nodeId] = node
                        node = self._extractNodeJson(val, node)
                        # Check node and complete node entry for nodestore
                        node, nodestore, counts = self._checkNode(node, nodestore, counts)

                        if head == val["id"]:
                            scope = clusterId

                # Entry is no cluster but ordinary node
                else:
                    nodeId= entry["id"]
                    node = node_mod.Node(self, nodeId)
                    node.__dict__["cluster"] = ""

                    nodestore[nodeId] = node
                    node = self._extractNodeJson(entry, node)
                    # Check node and complete node entry for nodestore
                    node, nodestore, counts = self._checkNode(node, nodestore, counts)
                    # Add node to the graph
                    self.overlay.addNode(nodeId)

                    if head == entry["id"]:
                        scope = nodeId

                if not node:
                    raise ConfigurationError("no node found in node.cfg")
                elif not scope:
                    raise ConfigurationError("Local node has no scope")

            # Parse connections between nodes
            if "connections" in data.keys():
                for val in data["connections"]:
                    self.overlay.addEdge(val["from"], val["to"])

        if not self.overlay.isConnected() or not self.overlay.isTree():
            raise ConfigurationError("Misconfigured overlay")

        # Current scope of this peer: local node and its cluster
        scope_list = {}
        # Dict of direct successors of this peer
        peers = {}

        peer_list = self.overlay.getSuccessors(scope)
        for key, value in nodestore.iteritems():
            if key == head or (scope and value.cluster == scope):
                scope_list[key] = nodestore[key]
            elif key in peer_list:
                peers[key] = nodestore[key]
            elif (value.cluster in peer_list and value.type == "manager"):
                peers[value.cluster] = nodestore[key]

        # Check if nodestore is valid
        self._checkNodeStore(scope_list)

        return scope_list, peers

    # Parses a node entry in Json format
    def _extractNodeJson(self, entry, node):
        for key in entry:
            node.__dict__[key] = entry[key]

        if (node.type != "manager" and node.type != "proxy" and node.type != "worker" and node.type != "standalone" and node.type != "peer"):
            raise ConfigurationError("strange node type detected, node " + node.name + " is " + str(node.type))

        return node

    # Parses broctl.cfg and returns a dictionary of all entries.
    def _readConfig(self, file):
        config = {}
        for line in open(file):

            line = line.strip()
            if not line or line.startswith("#"):
                continue

            args = line.split("=", 1)
            if len(args) != 2:
                raise ConfigurationError("%s: syntax error '%s'" % (file, line))

            (key, val) = args
            key = key.strip().lower()

            # if the key already exists, just overwrite with new value
            config[key] = val.strip()

        return config


    # Initialize a global option if not already set.
    def _setOption(self, key, val):
        key = key.lower()
        if key not in self.config:
            self.config[key] = self.subst(val)

    # Set a dynamic state variable.
    def _setState(self, key, val):
        key = key.lower()
        self.state[key] = val
        self.state_store.set(key, val)

    # Returns value of state variable, or None if it's not defined.
    def _getState(self, key):
        return self.state.get(key)

    # Read dynamic state variables from {$spooldir}/broctl.dat .
    def readState(self):
        self.state = dict(self.state_store.items())

    # Record the Bro version.
    def recordBroVersion(self):
        try:
            version = self._getBroVersion()
        except ConfigurationError:
            return False

        self._setState("broversion", version)
        self._setState("bro", self.subst("${bindir}/bro"))
        return True


    # Warn user to run broctl install if any config changes are detected.
    def warnBroctlInstall(self):
        # Check if Bro version is different from previously-installed version.
        if "broversion" in self.state:
            oldversion = self.state["broversion"]

            version = self._getBroVersion()

            if version != oldversion:
                self.ui.warn("new bro version detected (run the broctl \"restart --clean\" or \"install\" command)")
                return

        # Check if node config has changed since last install.
        if "hash-nodecfg" in self.state:
            nodehash = self.getNodeCfgHash()

            if nodehash != self.state["hash-nodecfg"]:
                self.ui.warn("broctl node config has changed (run the broctl \"restart --clean\" or \"install\" command)")
                self.warn_dangling_bro()
                return

        # Check if any config values have changed since last install.
        if "hash-broctlcfg" in self.state:
            cfghash = self.getBroctlCfgHash()
            if cfghash != self.state["hash-broctlcfg"]:
                self.ui.warn("broctl config has changed (run the broctl \"restart --clean\" or \"install\" command)")
                return


    # Warn if there might be any dangling Bro nodes (i.e., nodes that are
    # no longer part of the current node configuration, but that are still
    # running).
    def warn_dangling_bro(self):
        nodes = [ n.name for n in self.nodes() ]

        for key in self.state.keys():
            # Check if a PID is defined for a Bro node
            if key.endswith("-pid") and self._getState(key):
                nn = key[:-4]
                # Check if node name is in list of all known nodes
                if nn not in nodes:
                    hostkey = key.replace("-pid", "-host")
                    h = self._getState(hostkey)
                    if h:
                        self.ui.warn("Bro node \"%s\" possibly still running on host \"%s\" (PID %s)" % (nn, h, self._getState(key)))

    # Return a hash value (as a string) of the current broctl configuration.
    def getBroctlCfgHash(self):
        return str(hash(tuple(sorted(self.config.items()))))

    # Update the stored hash value of the current broctl configuration.
    def updateBroctlCfgHash(self):
        cfghash = self.getBroctlCfgHash()
        self._setState("hash-broctlcfg", cfghash)

    # Return a hash value (as a string) of the current broctl node config.
    def getNodeCfgHash(self):
        nn = []
        for n in self.nodes():
            nn.append(tuple([(key,val) for key,val in n.items() if not key.startswith("_")]))
        return str(hash(tuple(nn)))

    # Update the stored hash value of the current broctl node config.
    def updateNodeCfgHash(self):
        nodehash = self.getNodeCfgHash()
        self._setState("hash-nodecfg", nodehash)

    # Runs Bro to get its version number.
    def _getBroVersion(self):
        from BroControl import execute

        version = ""
        bro = self.subst("${bindir}/bro")
        if os.path.lexists(bro):
            (success, output) = execute.runLocalCmd("%s -v" % bro)
            if success and output:
                version = output[-1]
        else:
            raise ConfigurationError("cannot find Bro binary to determine version")

        m = re.search(".* version ([^ ]*).*$", version)
        if not m:
            raise ConfigurationError("cannot determine Bro version [%s]" % version.strip())

        version = m.group(1)
        # If bro is built with the "--enable-debug" configure option, then it
        # appends "-debug" to the version string.
        if version.endswith("-debug"):
            version = version[:-6]

        return version
