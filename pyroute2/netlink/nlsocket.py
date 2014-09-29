import io
import os
import struct
import socket
import threading

from pyroute2.iocore.addrpool import AddrPool  # FIXME: move to common
from pyroute2.netlink import nlmsg
from pyroute2.netlink import mtypes
from pyroute2.netlink import NetlinkError
from pyroute2.netlink import NetlinkDecodeError
from pyroute2.netlink import NetlinkHeaderDecodeError
from pyroute2.netlink import NLMSG_ERROR
from pyroute2.netlink import NETLINK_GENERIC


class Marshal(object):
    '''
    Generic marshalling class
    '''

    msg_map = {}
    debug = False

    def __init__(self):
        self.lock = threading.Lock()
        # one marshal instance can be used to parse one
        # message at once
        self.msg_map = self.msg_map or {}
        self.defragmentation = {}

    def parse(self, data, sock=None):
        '''
        Parse the data in the buffer

        If socket is provided, support defragmentation
        '''
        with self.lock:
            data.seek(0)
            offset = 0
            result = []

            if sock in self.defragmentation:
                save = self.defragmentation[sock]
                save.write(data.read())
                save.length += data.length
                # discard save
                data = save
                del self.defragmentation[sock]
                data.seek(0)

            while offset < data.length:
                # pick type and length
                (length, msg_type) = struct.unpack('IH', data.read(6))
                data.seek(offset)
                # if length + offset is greater than
                # remaining size, save the buffer for
                # defragmentation
                if (sock is not None) and (length + offset > data.length):
                    # create save buffer
                    self.defragmentation[sock] = save = io.BytesIO()
                    save.length = save.write(data.read())
                    # truncate data
                    data.truncate(offset)
                    break

                error = None
                if msg_type == NLMSG_ERROR:
                    data.seek(offset + 16)
                    code = abs(struct.unpack('i', data.read(4))[0])
                    if code > 0:
                        error = NetlinkError(code)
                    data.seek(offset)

                msg_class = self.msg_map.get(msg_type, nlmsg)
                msg = msg_class(data, debug=self.debug)
                try:
                    msg.decode()
                    msg['header']['error'] = error
                    # try to decode encapsulated error message
                    if error is not None:
                        enc_type = struct.unpack('H', msg.raw[24:26])[0]
                        enc_class = self.msg_map.get(enc_type, nlmsg)
                        enc = enc_class(msg.raw[20:])
                        enc.decode()
                        msg['header']['errmsg'] = enc
                except NetlinkHeaderDecodeError as e:
                    # in the case of header decoding error,
                    # create an empty message
                    msg = nlmsg()
                    msg['header']['error'] = e
                except NetlinkDecodeError as e:
                    msg['header']['error'] = e
                mtype = msg['header'].get('type', None)
                if mtype in (1, 2, 3, 4):
                    msg['event'] = mtypes.get(mtype, 'none')
                self.fix_message(msg)
                offset += msg.length
                result.append(msg)

            return result

    def fix_message(self, msg):
        pass


# 8<-----------------------------------------------------------
# Singleton, containing possible modifiers to the NetlinkSocket
# bind() call.
#
# Normally, you can open only one netlink connection for one
# process, but there is a hack. Current PID_MAX_LIMIT is 2^22,
# so we can use the rest to midify pid field.
#
# See also libnl library, lib/socket.c:generate_local_port()
sockets = AddrPool(minaddr=0x0,
                   maxaddr=0x3ff,
                   reverse=True)
# 8<-----------------------------------------------------------


class NetlinkSocket(socket.socket):
    '''
    Generic netlink socket
    '''

    def __init__(self, family=NETLINK_GENERIC, port=None, pid=None):
        socket.socket.__init__(self, socket.AF_NETLINK,
                               socket.SOCK_DGRAM, family)
        global sockets

        # 8<-----------------------------------------
        # PID init is here only for compatibility,
        # later it will be completely moved to bind()
        self.epid = None
        self.port = 0
        self.fixed = True
        if pid is None:
            self.pid = os.getpid() & 0x3fffff
            self.port = port
            self.fixed = self.port is not None
        elif pid == 0:
            self.pid = os.getpid()
        else:
            self.pid = pid
        # 8<-----------------------------------------
        self.groups = 0
        self.marshal = Marshal()

    def register_policy(self, policy, msg_class=None):
        '''
        Register netlink encoding/decoding policy. Can
        be specified in two ways:
        `nlsocket.register_policy(MSG_ID, msg_class)`
        to register one particular rule, or
        `nlsocket.register_policy({MSG_ID1: msg_class})`
        to register several rules at once.
        E.g.::

            policy = {RTM_NEWLINK: ifinfmsg,
                      RTM_DELLINK: ifinfmsg,
                      RTM_NEWADDR: ifaddrmsg,
                      RTM_DELADDR: ifaddrmsg}
            nlsocket.register_policy(policy)

        One can call `register_policy()` as many times,
        as one want to -- it will just extend the current
        policy scheme, not replace it.
        '''
        if isinstance(policy, int) and msg_class is not None:
            policy = {policy: msg_class}

        assert isinstance(policy, dict)
        for key in policy:
            self.marshal.msg_map[key] = policy[key]

        return self.marshal.msg_map

    def unregister_policy(self, policy):
        '''
        Unregister policy. Policy can be:

        * int -- then it will just remove one policy
        * list or tuple of ints -- remove all given
        * dict -- remove policies by keys from dict

        In the last case the routine will ignore dict values,
        it is implemented so just to make it compatible with
        `get_policy_map()` return value.
        '''
        if isinstance(policy, int):
            policy = [policy]
        elif isinstance(policy, dict):
            policy = list(policy)

        assert isinstance(policy, (tuple, list, set))

        for key in policy:
            del self.marshal.msg_map[key]

        return self.marshal.msg_map

    def get_policy_map(self, policy=None):
        '''
        Return policy for a given message type or for all
        message types. Policy parameter can be either int,
        or a list of ints. Always return dictionary.
        '''
        if policy is None:
            return self.marshal.msg_map

        if isinstance(policy, int):
            policy = [policy]

        assert isinstance(policy, (list, tuple, set))

        ret = {}
        for key in policy:
            ret[key] = self.marshal.msg_map[key]

        return ret

    def bind(self, groups=0, pid=None):
        '''
        Bind the socket to given multicast groups, using
        given pid.

        * If pid is None, use automatic port allocation
        * If pid == 0, use process' pid
        * If pid == <int>, use the value instead of pid
        '''
        if pid is not None:
            self.port = 0
            self.fixed = True
            self.pid = pid or os.getpid()

        self.groups = groups
        # if we have pre-defined port, use it strictly
        if self.fixed:
            self.epid = self.pid + (self.port << 22)
            socket.socket.bind(self, (self.epid, self.groups))
            return

        # if we have no pre-defined port, scan all the
        # range till the first available port
        for i in range(1024):
            try:
                self.port = sockets.alloc()
                self.epid = self.pid + (self.port << 22)
                socket.socket.bind(self, (self.epid, self.groups))
                # if we're here, bind() done successfully, just exit
                return
            except socket.error as e:
                # pass occupied sockets, raise other exceptions
                if e.errno != 98:
                    raise
        else:
            # raise "address in use" -- to be compatible
            raise socket.error(98, 'Address already in use')

    def get(self):
        '''
        Get parsed messages list.
        '''
        data = io.BytesIO()
        data.length = data.write(self.recv(16384))
        return self.marshal.parse(data, self)

    def close(self):
        '''
        Correctly close the socket and free all resources.
        '''
        global sockets
        if self.epid is not None:
            assert self.port is not None
            if not self.fixed:
                sockets.free(self.port)
            self.epid = None
        socket.socket.close(self)
