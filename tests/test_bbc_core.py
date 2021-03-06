# -*- coding: utf-8 -*-
import pytest

import binascii
import queue
import time

import sys
sys.path.extend(["../"])
from bbc_simple.core import bbclib
from bbc_simple.core.message_key_types import KeyType
from testutils import prepare, start_core_thread, get_core_client, make_client

LOGLEVEL = 'debug'
LOGLEVEL = 'info'

CURVE_TYPE = bbclib.KeyType.ECDSA_SECP256k1

core_num = 3
client_num = 3
cores = None
clients = None
domain_id = bbclib.get_new_id("testdomain")
asset_group_id = bbclib.get_new_id("asset_group_1")[:bbclib.DEFAULT_ID_LEN]
transaction = None
txid = None
user_id1 = bbclib.get_new_id("destination_id_test1")[:bbclib.DEFAULT_ID_LEN]
txid1 = bbclib.get_new_id("dummy_txid_1")[:bbclib.DEFAULT_ID_LEN]

result_queue = queue.Queue()
keypair = bbclib.KeyPair(curvetype=CURVE_TYPE)
keypair.generate()


def wait_results(count):
    total = 0
    for i in range(count):
        total += result_queue.get()
    return total


def dummy_send_message(data):
    print("[Core] recv=%s" % data)
    if KeyType.reason in data:
        result_queue.put(0)
    else:
        result_queue.put(1)


class TestBBcCore(object):

    def test_01_setup(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")

        prepare(core_num=core_num, client_num=client_num, loglevel=LOGLEVEL)
        for i in range(core_num):
            start_core_thread(index=i, core_port_increment=i)
        time.sleep(1)
        for i in range(client_num):
            make_client(index=i, core_port_increment=i, connect_to_core=False)

        global cores, clients
        cores, clients = get_core_client()
        for i in range(core_num):
            cores[i].networking.create_domain(domain_id=domain_id)

    def test_03_transaction_insert(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")
        global transaction
        print("-- insert transaction only at core_node_1 --")
        user1 = clients[1]['user_id']
        transaction = bbclib.make_transaction(event_num=2, witness=True)
        transaction.events[0].add(mandatory_approver=clients[1]['user_id'])
        bbclib.add_event_asset(transaction, event_idx=0, asset_group_id=asset_group_id,
                               user_id=user1, asset_body=b'123456')
        bbclib.add_event_asset(transaction, event_idx=1, asset_group_id=asset_group_id,
                               user_id=user1, asset_body=b'abcdefg')
        transaction.witness.add_witness(user_id=user1)

        sig = transaction.sign(keypair=clients[1]['keypair'])
        transaction.witness.add_signature(user_id=user1, signature=sig)
        transaction.digest()
        print(transaction)
        print("register transaction=", binascii.b2a_hex(transaction.transaction_id))
        ret = cores[1].insert_transaction(domain_id, transaction.serialize())
        assert ret[KeyType.transaction_id] == transaction.transaction_id

    def test_04_1__search_transaction_by_txid(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")
        ret = cores[1]._search_transaction_by_txid(domain_id, transaction.transaction_id)
        assert ret is not None

    def test_04_2_search_asset_by_asid(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")
        asid = transaction.events[0].asset.asset_id
        ret = cores[1].search_transaction_with_condition(domain_id, asset_group_id=asset_group_id, asset_id=asid)
        print(ret)
        assert ret is not None

    def test_04_3_search_asset_by_asid_locally_in_storage(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")
        asid = transaction.events[1].asset.asset_id
        ret = cores[1].search_transaction_with_condition(domain_id, asset_group_id=asset_group_id, asset_id=asid)
        print(ret)
        assert ret is not None

    def test_05_1__search_transaction_by_txid_other_node_not_found(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")

        print("-- insert transaction only at core_node_2 --")
        global transaction
        user1 = clients[2]['user_id']
        transaction = bbclib.make_transaction(event_num=2, witness=True)
        bbclib.add_event_asset(transaction, event_idx=0, asset_group_id=asset_group_id,
                               user_id=user1, asset_body=b'aaddbbdd')
        bbclib.add_event_asset(transaction, event_idx=1, asset_group_id=asset_group_id,
                               user_id=user1, asset_body=b'112423')
        for i, user in enumerate(clients):
            transaction.witness.add_witness(user_id=clients[i]['user_id'])
        for i, user in enumerate(clients):
            sig = transaction.sign(keypair=clients[i]['keypair'])
            transaction.witness.add_signature(user_id=clients[i]['user_id'], signature=sig)
        transaction.digest()
        print(transaction)
        print("register transaction=", binascii.b2a_hex(transaction.transaction_id))
        ret = cores[2].insert_transaction(domain_id, transaction.serialize())
        assert KeyType.transaction_id in ret
        assert ret[KeyType.transaction_id] == transaction.transaction_id

        # -- search the transaction at core_node_0
        ret = cores[0]._search_transaction_by_txid(domain_id, transaction.transaction_id)
        assert ret is not None  # DB is shared among all cores

    def test_05_2_search_asset_by_asid_other_node_not_found(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")
        # -- search the asset at core_node_0
        asid = transaction.events[0].asset.asset_id
        ret = cores[0].search_transaction_with_condition(domain_id, asset_group_id=asset_group_id, asset_id=asid)
        assert ret is not None
        print(ret)

    def test_07_1_insert_transaction(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")
        global transaction
        transaction = bbclib.BBcTransaction()
        rtn = bbclib.BBcRelation()
        rtn.asset_group_id = asset_group_id
        rtn.asset = bbclib.BBcAsset()
        rtn.asset.add(user_id=user_id1, asset_body=b'bbbbbb')
        ptr = bbclib.BBcPointer()
        ptr.add(transaction_id=txid1)
        rtn.add(pointer=ptr)
        wit = bbclib.BBcWitness()
        transaction.add(relation=rtn, witness=wit)
        wit.add_witness(user_id1)
        sig = transaction.sign(key_type=CURVE_TYPE,
                               private_key=keypair.private_key, public_key=keypair.public_key)
        transaction.add_signature(user_id=user_id1, signature=sig)
        transaction.digest()
        ret = cores[0].insert_transaction(domain_id, transaction.serialize())
        assert ret[KeyType.transaction_id] == transaction.transaction_id

        ret = cores[0]._search_transaction_by_txid(domain_id, transaction.transaction_id)
        assert ret is not None
        print(ret)

        print("-- wait 2 seconds --")
        #time.sleep(2)

    def test_07_2__search_transaction_by_txid_other_node(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")
        # -- search the transaction at core_node_1
        ret = cores[1]._search_transaction_by_txid(domain_id, transaction.transaction_id)
        assert ret is not None
        print(ret)

    def test_07_2_search_asset_by_userid_other_node(self):
        print("\n-----", sys._getframe().f_code.co_name, "-----")
        # -- search the asset at core_node_2
        asid = transaction.relations[0].asset.asset_id
        ret = cores[2].search_transaction_with_condition(domain_id, asset_group_id=asset_group_id, user_id=user_id1)
        print(ret)
        assert ret is not None


if __name__ == '__main__':
    pytest.main()
