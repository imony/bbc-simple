#!/bin/sh
""":" .

exec python "$0" "$@"
"""
# -*- coding: utf-8 -*-
"""
Copyright (c) 2018 quvox.net.

This code is based on that in bbc-1 (https://github.com/beyond-blockchain/bbc1.git)

"""
from gevent import monkey
monkey.patch_all()
from gevent.pool import Pool
from gevent.server import StreamServer
import socket as py_socket
from gevent.socket import wait_read
import gevent
import os
import signal
import logging
import binascii
import json
import traceback
import copy
from argparse import ArgumentParser

import sys
sys.path.extend(["../../"])
from bbc_simple.core import bbclib
from bbc_simple.core.message_key_types import KeyType, to_2byte
from bbc_simple.core.bbclib import BBcTransaction, MsgType
from bbc_simple.core import bbc_network, user_message_routing, message_key_types
from bbc_simple.core import query_management, bbc_stats
from bbc_simple.core.bbc_config import BBcConfig
from bbc_simple.core.bbc_error import *
from bbc_simple.logger.fluent_logger import initialize_logger

from bbc_simple.core.bbc_config import DEFAULT_CORE_PORT


VERSION = "bbc_simple v0.1"

PID_FILE = "/tmp/bbc_simple.pid"
POOL_SIZE = 1000
DEFAULT_ANYCAST_TTL = 5
TX_TRAVERSAL_MAX = 30

ticker = query_management.get_ticker()
core_service = None


def _make_message_structure(domain_id, cmd, dstid, qid):
    """Create a base structure of message

    Args:
        domain_id (bytes): the target domain_id
        cmd (bytes): command type in message_key_types.KeyType
        dstid (bytes): destination user_id
        qid (bytes): query_id to include in the message
    Returns:
        dict: message
    """
    return {
        KeyType.domain_id: domain_id,
        KeyType.command: cmd,
        KeyType.destination_user_id: dstid,
        KeyType.query_id: qid,
        KeyType.status: ESUCCESS,
    }


def _create_search_result(txobj_dict):
    """Create transaction search result"""
    response_info = dict()
    for txid, txobj in txobj_dict.items():
        if txid != txobj.transaction_id:
            response_info.setdefault(KeyType.compromised_transactions, list()).append(txobj.transaction_data)
            continue
        if bbclib.validate_transaction_object(txobj):
            response_info.setdefault(KeyType.transactions, list()).append(txobj.transaction_data)
        else:
            response_info.setdefault(KeyType.compromised_transactions, list()).append(txobj.transaction_data)
    return response_info


class BBcCoreService:
    """Base service object of BBc-1"""
    def __init__(self, core_port=None, workingdir=".bbc1", configfile=None, ipv6=False,
                 server_start=True, default_conffile=None, logconf=None):
        if logconf is not None:
            initialize_logger(logconf)
        self.logger = logging.getLogger("bbc_core")
        self.stats = bbc_stats.BBcStats()
        self.config = BBcConfig(workingdir, configfile, default_conffile)
        conf = self.config.get_config()
        self.ipv6 = ipv6
        self.logger.debug("config = %s" % conf)
        self.networking = bbc_network.BBcNetwork(self.config, core=self)
        for domain_id_str in conf['domains'].keys():
            domain_id = bbclib.convert_idstring_to_bytes(domain_id_str)
            c = self.config.get_domain_config(domain_id)
            self.networking.create_domain(domain_id=domain_id, config=c)

        gevent.signal(signal.SIGINT, self.quit_program)
        if server_start:
            self._start_server(core_port)

    def quit_program(self):
        """Processes when quiting program"""
        self.config.update_config()
        os._exit(0)

    def _start_server(self, port):
        """Start TCP(v4 or v6) server"""
        pool = Pool(POOL_SIZE)
        if self.ipv6:
            server = StreamServer(("::", port), self._handler, spawn=pool)
        else:
            server = StreamServer(("0.0.0.0", port), self._handler, spawn=pool)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

    def _error_reply(self, msg=None, err_code=EINVALID_COMMAND, txt=""):
        """Create and send error reply message

        Args:
            msg (dict): message to send
            err_code (int): error code defined in bbc_error.py
            txt (str): error message
        Returns:
            bool:
        """
        msg[KeyType.status] = err_code
        msg[KeyType.reason] = txt
        domain_id = msg[KeyType.domain_id]
        if domain_id in self.networking.domains:
            self.networking.domains[domain_id]['user'].send_message_to_user(msg)
            return True
        else:
            return False

    def _handler(self, socket, address):
        """Message wait loop for a client"""
        self.stats.update_stats_increment("client", "total_num", 1)
        user_info = None
        msg_parser = message_key_types.Message()
        try:
            while True:
                wait_read(socket.fileno())
                buf = socket.recv(8192)
                if len(buf) == 0:
                    break
                msg_parser.recv(buf)
                while True:
                    msg = msg_parser.parse()
                    if msg is None:
                        break
                    disconnection, new_info = self._process(socket, msg, msg_parser.payload_type)
                    if disconnection:
                        break
                    if new_info is not None:
                        user_info = new_info
        except Exception as e:
            self.logger.info("TCP disconnect: %s" % e)
            traceback.print_exc()
        self.logger.debug("closing socket")
        if user_info is not None:
            self.networking.domains[user_info[0]]['user'].unregister_user(user_info[1], socket)
        try:
            socket.shutdown(py_socket.SHUT_RDWR)
            socket.close()
        except:
            pass
        self.logger.debug("connection closed")
        self.stats.update_stats_decrement("client", "total_num", 1)

    def _param_check(self, param, dat):
        """Check if the param is included

        Args:
            param (bytes|list): Commands that must be included in the message
            dat (dict): received message
        Returns:
            bool: True if check is successful
        """
        if isinstance(param, list):
            for p in param:
                if p not in dat:
                    self._error_reply(msg=dat, err_code=EINVALID_COMMAND, txt="lack of mandatory params")
                    return False
        else:
            if param not in dat:
                self._error_reply(msg=dat, err_code=EINVALID_COMMAND, txt="lack of mandatory params")
                return False
        return True

    def _process(self, socket, dat, payload_type):
        """Process received message

        Args:
            socket (Socket): server socket
            dat (dict): received message
            payload_type (bytes): PayloadType value of msg
        Returns:
            bool: True if disconnection is detected
            list: return user info (domain_id, user_id) when a new user_id is coming
        """
        self.stats.update_stats_increment("client", "num_message_receive", 1)
        #self.logger.debug("process message from %s: %s" % (binascii.b2a_hex(dat[KeyType.source_user_id]), dat))
        if not self._param_check([KeyType.command, KeyType.source_user_id], dat):
            self.logger.debug("message has bad format")
            return False, None

        domain_id = dat.get(KeyType.domain_id, None)
        umr = None
        if domain_id is not None:
            if domain_id in self.networking.domains:
                umr = self.networking.domains[domain_id]['user']
            else:
                umr = user_message_routing.UserMessageRoutingDummy(networking=self.networking, domain_id=domain_id)

        cmd = dat[KeyType.command]
        if cmd == MsgType.REQUEST_SEARCH_TRANSACTION:
            if not self._param_check([KeyType.domain_id, KeyType.transaction_id], dat):
                self.logger.debug("REQUEST_SEARCH_TRANSACTION: bad format")
                return False, None
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_SEARCH_TRANSACTION,
                                            dat[KeyType.source_user_id], dat[KeyType.query_id])
            txinfo = self._search_transaction_by_txid(domain_id, dat[KeyType.transaction_id])
            if txinfo is None:
                if not self._error_reply(msg=retmsg, err_code=ENOTRANSACTION, txt="Cannot find transaction"):
                    user_message_routing.direct_send_to_user(socket, retmsg)
                return False, None
            if KeyType.compromised_transaction_data in txinfo:
                retmsg[KeyType.status] = EBADTRANSACTION
            retmsg.update(txinfo)
            umr.send_message_to_user(retmsg)

        elif cmd == MsgType.REQUEST_SEARCH_WITH_CONDITIONS:
            if not self._param_check([KeyType.domain_id], dat):
                self.logger.debug("REQUEST_SEARCH_WITH_CONDITIONS: bad format")
                return False, None
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_SEARCH_WITH_CONDITIONS,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            txinfo = self.search_transaction_with_condition(domain_id,
                                                            asset_group_id=dat.get(KeyType.asset_group_id, None),
                                                            asset_id=dat.get(KeyType.asset_id, None),
                                                            user_id=dat.get(KeyType.user_id, None),
                                                            count=dat.get(KeyType.count, 1),
                                                            direction=dat.get(KeyType.direction, 0))
            if txinfo is None or KeyType.transactions not in txinfo:
                if not self._error_reply(msg=retmsg, err_code=ENOTRANSACTION, txt="Cannot find transaction"):
                    user_message_routing.direct_send_to_user(socket, retmsg)
            else:
                retmsg.update(txinfo)
                umr.send_message_to_user(retmsg)

        elif cmd == MsgType.REQUEST_COUNT_TRANSACTIONS:
            if not self._param_check([KeyType.domain_id], dat):
                self.logger.debug("REQUEST_COUNT_TRANSACTIONS: bad format")
                return False, None
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_COUNT_TRANSACTIONS,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            count = self.count_transactions(domain_id, asset_group_id=dat.get(KeyType.asset_group_id, None),
                                            asset_id=dat.get(KeyType.asset_id, None),
                                            user_id=dat.get(KeyType.user_id, None))
            retmsg[KeyType.count] = count
            umr.send_message_to_user(retmsg)

        elif cmd == MsgType.REQUEST_TRAVERSE_TRANSACTIONS:
            if not self._param_check([KeyType.domain_id, KeyType.transaction_id,
                                     KeyType.direction, KeyType.hop_count], dat):
                self.logger.debug("REQUEST_TRAVERSE_TRANSACTIONS: bad format")
                return False, None
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_TRAVERSE_TRANSACTIONS,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            retmsg[KeyType.transaction_id] = dat[KeyType.transaction_id]
            asset_group_id = dat.get(KeyType.asset_group_id, None)
            user_id = dat.get(KeyType.user_id, None)
            all_included, txtree = self._traverse_transactions(domain_id, dat[KeyType.transaction_id],
                                                               asset_group_id=asset_group_id, user_id=user_id,
                                                               direction=dat[KeyType.direction],
                                                               hop_count=dat[KeyType.hop_count])
            if txtree is None or len(txtree) == 0:
                if not self._error_reply(msg=retmsg, err_code=ENOTRANSACTION, txt="Cannot find transaction"):
                    user_message_routing.direct_send_to_user(socket, retmsg)
            else:
                retmsg[KeyType.transaction_tree] = txtree
                retmsg[KeyType.all_included] = all_included
                umr.send_message_to_user(retmsg)

        elif cmd == MsgType.REQUEST_GATHER_SIGNATURE:
            if not self._param_check([KeyType.domain_id, KeyType.transaction_data], dat):
                self.logger.debug("REQUEST_GATHER_SIGNATURE: bad format")
                return False, None
            if not self._distribute_transaction_to_gather_signatures(dat[KeyType.domain_id], dat):
                retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_GATHER_SIGNATURE,
                                                 dat[KeyType.source_user_id], dat[KeyType.query_id])
                if not self._error_reply(msg=retmsg, err_code=EINVALID_COMMAND, txt="Fail to forward transaction"):
                    user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_INSERT:
            if not self._param_check([KeyType.domain_id, KeyType.transaction_data], dat):
                self.logger.debug("REQUEST_INSERT: bad format")
                return False, None
            transaction_data = dat[KeyType.transaction_data]
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_INSERT,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            ret = self.insert_transaction(dat[KeyType.domain_id], transaction_data)
            if isinstance(ret, str):
                if not self._error_reply(msg=retmsg, err_code=EINVALID_COMMAND, txt=ret):
                    user_message_routing.direct_send_to_user(socket, retmsg)
            else:
                retmsg.update(ret)
                umr.send_message_to_user(retmsg)

        elif cmd == MsgType.RESPONSE_SIGNATURE:
            if not self._param_check([KeyType.domain_id, KeyType.destination_user_id, KeyType.source_user_id], dat):
                self.logger.debug("RESPONSE_SIGNATURE: bad format")
                return False, None
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_GATHER_SIGNATURE,
                                             dat[KeyType.destination_user_id], dat[KeyType.query_id])
            if KeyType.signature in dat:
                retmsg[KeyType.transaction_data_format] = dat[KeyType.transaction_data_format]
                retmsg[KeyType.signature] = dat[KeyType.signature]
                retmsg[KeyType.ref_index] = dat[KeyType.ref_index]
            elif KeyType.status not in dat:
                retmsg[KeyType.status] = EOTHER
                retmsg[KeyType.reason] = dat[KeyType.reason]
            elif dat[KeyType.status] < ESUCCESS:
                retmsg[KeyType.status] = dat[KeyType.status]
                retmsg[KeyType.reason] = dat[KeyType.reason]
            retmsg[KeyType.source_user_id] = dat[KeyType.source_user_id]
            umr.send_message_to_user(retmsg)

        elif cmd == MsgType.MESSAGE:
            if not self._param_check([KeyType.domain_id, KeyType.source_user_id, KeyType.destination_user_id], dat):
                self.logger.debug("MESSAGE: bad format")
                return False, None
            if KeyType.is_anycast in dat:
                dat[KeyType.anycast_ttl] = DEFAULT_ANYCAST_TTL
            umr.send_message_to_user(dat)

        elif cmd == MsgType.REGISTER:
            if domain_id is None:
                return False, None
            if not self._param_check([KeyType.domain_id, KeyType.source_user_id], dat):
                self.logger.debug("REGISTER: bad format")
                return False, None
            user_id = dat[KeyType.source_user_id]
            self.logger.debug("[%s] register_user: %s" % (binascii.b2a_hex(domain_id[:2]),
                                                          binascii.b2a_hex(user_id[:4])))
            umr.register_user(user_id, socket, on_multiple_nodes=dat.get(KeyType.on_multinodes, False))
            return False, (domain_id, user_id)

        elif cmd == MsgType.UNREGISTER:
            if umr is not None:
                umr.unregister_user(dat[KeyType.source_user_id], socket)
            return True, None

        elif cmd == MsgType.REQUEST_INSERT_NOTIFICATION:
            umr.register_notification(dat[KeyType.asset_group_id], dat[KeyType.source_user_id])

        elif cmd == MsgType.CANCEL_INSERT_NOTIFICATION:
            umr.unregister_notification(dat[KeyType.asset_group_id], dat[KeyType.source_user_id])

        elif cmd == MsgType.REQUEST_GET_STATS:
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_GET_STATS,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            retmsg[KeyType.stats] = copy.deepcopy(self.stats.get_stats())
            user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_GET_CONFIG:
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_GET_CONFIG,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            jsondat = self.config.get_json_config()
            retmsg[KeyType.bbc_configuration] = jsondat
            user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_GET_DOMAINLIST:
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_GET_DOMAINLIST,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            data = bytearray()
            data.extend(to_2byte(len(self.networking.domains)))
            for domain_id in self.networking.domains:
                data.extend(domain_id)
            retmsg[KeyType.domain_list] = bytes(data)
            user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_GET_USERS:
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_GET_USERS,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            data = bytearray()
            data.extend(to_2byte(len(umr.registered_users)))
            for user_id in umr.registered_users.keys():
                data.extend(user_id)
            retmsg[KeyType.user_list] = bytes(data)
            user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_GET_NODEID:
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_GET_NODEID,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            data = bytearray(self.networking.domains[domain_id]['node_id'])
            retmsg[KeyType.node_id] = bytes(data)
            user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_GET_NOTIFICATION_LIST:
            retmsg = _make_message_structure(domain_id, MsgType.RESPONSE_GET_NOTIFICATION_LIST,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            data = bytearray()
            if umr is None or isinstance(umr, user_message_routing.UserMessageRoutingDummy):
                retmsg[KeyType.result] = EINVALID_COMMAND
            else:
                data.extend(to_2byte(len(umr.insert_notification_list)))
                for asset_group_id in umr.insert_notification_list.keys():
                    data.extend(asset_group_id)
                    data.extend(to_2byte(len(umr.insert_notification_list[asset_group_id])))
                    for user_id in umr.insert_notification_list[asset_group_id]:
                        data.extend(user_id)
            retmsg[KeyType.notification_list] = bytes(data)
            user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_SETUP_DOMAIN:
            if not self._param_check([KeyType.domain_id], dat):
                self.logger.debug("REQUEST_SETUP_DOMAIN: bad format")
                return False, None
            conf = None
            if KeyType.bbc_configuration in dat:
                conf = json.loads(dat[KeyType.bbc_configuration])
            retmsg = _make_message_structure(None, MsgType.RESPONSE_SETUP_DOMAIN,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            retmsg[KeyType.result] = self.networking.create_domain(domain_id=domain_id, config=conf)
            if not retmsg[KeyType.result]:
                retmsg[KeyType.reason] = "Already exists"
            retmsg[KeyType.domain_id] = domain_id
            user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_CLOSE_DOMAIN:
            retmsg = _make_message_structure(None, MsgType.RESPONSE_CLOSE_DOMAIN,
                                             dat[KeyType.source_user_id], dat[KeyType.query_id])
            retmsg[KeyType.result] = self.networking.remove_domain(domain_id)
            if not retmsg[KeyType.result]:
                retmsg[KeyType.reason] = "No such domain"
            user_message_routing.direct_send_to_user(socket, retmsg)

        elif cmd == MsgType.REQUEST_GET_STORED_MESSAGES:
            qid = dat[KeyType.query_id]
            if KeyType.request_async in dat:
                qid = None
            umr.get_stored_messages(dat[KeyType.source_user_id], qid)

        else:
            self.logger.error("Bad command/response: %s" % cmd)
        return False, None

    def validate_transaction(self, txdata):
        """Validate transaction by verifying signature

        Args:
            txdata (bytes): serialized transaction data
        Returns:
            BBcTransaction: if validation fails, None returns.
        """
        txobj = BBcTransaction()
        if not txobj.deserialize(txdata):
            self.stats.update_stats_increment("transaction", "invalid", 1)
            self.logger.error("Fail to deserialize transaction data")
            return None
        txobj.digest()

        flag, valid_asset, invalid_asset = bbclib.validate_transaction_object(txobj)
        if flag:
            return txobj
        else:
            self.stats.update_stats_increment("transaction", "invalid", 1)
            return None

    def insert_transaction(self, domain_id, txdata):
        """Insert transaction into ledger

        Args:
            domain_id (bytes): target domain_id
            txdata (bytes): serialized transaction data
        Returns:
            dict|str: inserted transaction_id or error message
        """
        self.stats.update_stats_increment("transaction", "insert_count", 1)
        if domain_id is None:
            self.stats.update_stats_increment("transaction", "insert_fail_count", 1)
            self.logger.error("No such domain")
            return "Set up the domain, first!"
        txobj = self.validate_transaction(txdata)
        if txobj is None:
            self.stats.update_stats_increment("transaction", "insert_fail_count", 1)
            self.logger.error("Bad transaction format")
            return "Bad transaction format"
        self.logger.debug("[node:%s] insert_transaction %s" %
                          (self.networking.domains[domain_id]['name'], binascii.b2a_hex(txobj.transaction_id[:4])))

        asset_group_ids = self.networking.domains[domain_id]['data'].insert_transaction(txdata, txobj=txobj)
        if asset_group_ids is None:
            self.stats.update_stats_increment("transaction", "insert_fail_count", 1)
            self.logger.error("[%s] Fail to insert a transaction into the ledger" % self.networking.domains[domain_id]['name'])
            return "Failed to insert a transaction into the ledger"

        self.send_inserted_notification(domain_id, asset_group_ids, txobj.transaction_id)

        return {KeyType.transaction_id: txobj.transaction_id}

    def send_inserted_notification(self, domain_id, asset_group_ids, transaction_id):
        """Broadcast NOTIFY_INSERTED

        Args:
            domain_id (bytes): target domain_id
            asset_group_ids (list): list of asset_group_ids
            transaction_id (bytes): transaction_id that has just inserted
        """
        msg = bytearray()
        msg.extend(int(len(transaction_id)).to_bytes(1, 'big'))
        msg.extend(int(len(domain_id)).to_bytes(1, 'big'))
        msg.extend(transaction_id)
        msg.extend(domain_id)
        msg.extend(int(len(asset_group_ids)).to_bytes(1, 'big'))
        for asset_group_id in asset_group_ids:
            msg.extend(asset_group_id)
        self.networking.broadcast_notification_message(domain_id=domain_id, msg=bytes(msg))

    def _distribute_transaction_to_gather_signatures(self, domain_id, dat):
        """Request to distribute sign_request to users

        Args:
            domain_id (bytes): target domain_id
            dat (dict): message to send
        Returns:
            bool: True
        """
        destinations = dat[KeyType.destination_user_ids]
        msg = _make_message_structure(domain_id, MsgType.REQUEST_SIGNATURE, None, dat[KeyType.query_id])
        msg[KeyType.source_user_id] = dat[KeyType.source_user_id]
        umr = self.networking.domains[domain_id]['user']
        for dst in destinations:
            if dst == dat[KeyType.source_user_id]:
                continue
            msg[KeyType.destination_user_id] = dst
            if KeyType.hint in dat:
                msg[KeyType.hint] = dat[KeyType.hint]
            msg[KeyType.transaction_data] = dat[KeyType.transaction_data]
            if KeyType.transactions in dat:
                msg[KeyType.transactions] = dat[KeyType.transactions]
            umr.send_message_to_user(msg)
        return True

    def _search_transaction_by_txid(self, domain_id, transaction_id):
        """Search transaction_data by transaction_id

        Args:
            domain_id (bytes): target domain_id
            transaction_id (bytes): transaction_id to search
        Returns:
            dict: dictionary having transaction_id, serialized transaction data, asset files
        """
        self.stats.update_stats_increment("transaction", "search_count", 1)
        if domain_id is None:
            self.logger.error("No such domain")
            return None
        if transaction_id is None:
            self.logger.error("Transaction_id must not be None")
            return None

        dh = self.networking.domains[domain_id]['data']
        ret_txobj = dh.search_transaction(transaction_id=transaction_id)
        if ret_txobj is None or len(ret_txobj) == 0:
            return None

        response_info = _create_search_result(ret_txobj)
        response_info[KeyType.transaction_id] = transaction_id
        if KeyType.transactions in response_info:
            response_info[KeyType.transaction_data] = response_info[KeyType.transactions][0]
            del response_info[KeyType.transactions]
        elif KeyType.compromised_transactions in response_info:
            response_info[KeyType.compromised_transaction_data] = response_info[KeyType.compromised_transactions][0]
            del response_info[KeyType.compromised_transactions]
        return response_info

    def search_transaction_with_condition(self, domain_id, asset_group_id=None, asset_id=None, user_id=None,
                                          direction=0, count=1):
        """Search transactions that match given conditions

        When Multiple conditions are given, they are considered as AND condition.

        Args:
            domain_id (bytes): target domain_id
            asset_group_id (bytes): asset_group_id that target transactions should have
            asset_id (bytes): asset_id that target transactions should have
            user_id (bytes): user_id that target transactions should have
            direction (int): 0: descend, 1: ascend
            count (int): The maximum number of transactions to retrieve
        Returns:
            dict: dictionary having transaction_id, serialized transaction data, asset files
        """
        if domain_id is None:
            self.logger.error("No such domain")
            return None

        dh = self.networking.domains[domain_id]['data']
        ret_txobj = dh.search_transaction(asset_group_id=asset_group_id, asset_id=asset_id, user_id=user_id,
                                          direction=direction, count=count)
        if ret_txobj is None or len(ret_txobj) == 0:
            return None

        return _create_search_result(ret_txobj)

    def count_transactions(self, domain_id, asset_group_id=None, asset_id=None, user_id=None):
        """Count transactions that match given conditions

        When Multiple conditions are given, they are considered as AND condition.

        Args:
            domain_id (bytes): target domain_id
            asset_group_id (bytes): asset_group_id that target transactions should have
            asset_id (bytes): asset_id that target transactions should have
            user_id (bytes): user_id that target transactions should have
        Returns:
            int: the number of transactions
        """
        if domain_id is None:
            self.logger.error("No such domain")
            return None

        dh = self.networking.domains[domain_id]['data']
        return dh.count_transactions(asset_group_id=asset_group_id, asset_id=asset_id, user_id=user_id)

    def _traverse_transactions(self, domain_id, transaction_id, asset_group_id=None, user_id=None, direction=1, hop_count=3):
        """Get transaction tree from the specified transaction_id and given conditions

        If both asset_group_id and user_id are specified, they are treated as AND condition.
        Transaction tree in the return values are in the following format:
        [ [list of serialized transactions in 1-hop from the base], [list of serialized transactions in 2-hop from the base],,,,

        Args:
            domain_id (bytes): target domain_id
            transaction_id (bytes): the base transaction_id from which traverse starts
            asset_group_id (bytes): asset_group_id that target transactions should have
            user_id (bytes): user_id that target transactions should have
            direction (int): 1:backward, non-1:forward
            hop_count (bytes): hop count to traverse
        Returns:
            list: list of [include_all_flag, transaction tree]
        """
        self.stats.update_stats_increment("transaction", "search_count", 1)
        if domain_id is None:
            self.logger.error("No such domain")
            return None
        if transaction_id is None:
            self.logger.error("Transaction_id must not be None")
            return None

        dh = self.networking.domains[domain_id]['data']
        txtree = list()

        traverse_to_past = True if direction == 1 else False
        tx_count = 0
        txids = dict()
        current_txids = [transaction_id]
        include_all_flag = True
        if hop_count > TX_TRAVERSAL_MAX * 2:
            hop_count = TX_TRAVERSAL_MAX * 2
            include_all_flag = False
        for i in range(hop_count):
            tx_brothers = list()
            next_txids = list()
            #print("### txcount=%d, len(current_txids)=%d" % (tx_count, len(current_txids)))
            if tx_count + len(current_txids) > TX_TRAVERSAL_MAX:  # up to 30 entries
                include_all_flag = False
                break
            #print("[%d] current_txids:%s" % (i, [d.hex() for d in current_txids]))
            for txid in current_txids:
                if txid in txids:
                    continue
                tx_count += 1
                txids[txid] = True
                ret_txobj = dh.search_transaction(transaction_id=txid)
                if ret_txobj is None or len(ret_txobj) == 0:
                    continue
                if asset_group_id is not None or user_id is not None:
                    flag = False
                    for asgid, asset_id, uid in dh.get_asset_info(ret_txobj[txid]):
                        flag = True
                        if asset_group_id is not None and asgid != asset_group_id:
                            flag = False
                        if user_id is not None and uid != user_id:
                            flag = False
                        if flag:
                            break
                    if not flag:
                        continue
                tx_brothers.append(ret_txobj[txid].transaction_data)

                ret = dh.search_transaction_topology(transaction_id=txid, traverse_to_past=traverse_to_past)
                #print("txid=%s: (%d) ret=%s" % (txid.hex(), len(ret), ret))
                if ret is not None:
                    for topology in ret:
                        if traverse_to_past:
                            next_txid = topology[2]
                        else:
                            next_txid = topology[1]
                        if next_txid not in txids:
                            next_txids.append(next_txid)
            if len(tx_brothers) > 0:
                txtree.append(tx_brothers)
            current_txids = next_txids

        return include_all_flag, txtree


def daemonize(pidfile=PID_FILE):
    """Run in background"""
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    os.setsid()
    pid = os.fork()
    if pid > 0:
        f2 = open(pidfile, 'w')
        f2.write(str(pid)+"\n")
        f2.close()
        os._exit(0)
    os.umask(0)


def parser():
    usage = 'python {} [--coreport <number>] [--workingdir <dir>] [--config <filename>] ' \
            '[-6] [--daemon] [--kill] [--help]'.format(__file__)
    argparser = ArgumentParser(usage=usage)
    argparser.add_argument('-cp', '--coreport', type=int, default=DEFAULT_CORE_PORT, help='waiting TCP port')
    argparser.add_argument('-w', '--workingdir', type=str, default=".bbc1", help='working directory name')
    argparser.add_argument('-c', '--config', type=str, default=None, help='config file name')
    argparser.add_argument('--default_config', type=str, default=None, help='default config file')
    argparser.add_argument('--log_config', type=str, default=None, help='log conf file')
    argparser.add_argument('-6', '--ipv6', action='store_true', default=False, help='Use IPv6 for waiting TCP connection')
    argparser.add_argument('-d', '--daemon', action='store_true', help='run in background')
    argparser.add_argument('-k', '--kill', action='store_true', help='kill the daemon')
    args = argparser.parse_args()
    return args


if __name__ == '__main__':
    argresult = parser()
    if argresult.kill:
        import subprocess
        import sys
        subprocess.call("kill `cat " + PID_FILE + "`", shell=True)
        subprocess.call("rm -f " + PID_FILE, shell=True)
        sys.exit(0)
    if argresult.daemon:
        daemonize()
    BBcCoreService(
        core_port=argresult.coreport,
        workingdir=argresult.workingdir,
        configfile=argresult.config,
        ipv6=argresult.ipv6,
        default_conffile=argresult.default_config,
        logconf=argresult.log_config,# "../logger/logconf.yml"
    )
