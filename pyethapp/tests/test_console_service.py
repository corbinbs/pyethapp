from itertools import count
import pytest
import serpent
from devp2p.peermanager import PeerManager
import ethereum
from ethereum import tester
from ethereum.ethpow import mine
import ethereum.keys
import ethereum.config
from ethereum.slogging import get_logger
from pyethapp.accounts import Account, AccountsService, mk_random_privkey
from pyethapp.app import EthApp
from pyethapp.config import update_config_with_defaults, get_default_config
from pyethapp.db_service import DBService
from pyethapp.eth_service import ChainService
from pyethapp.jsonrpc import JSONRPCServer
from pyethapp.pow_service import PoWService
from pyethapp.console_service import Console

# reduce key derivation iterations
ethereum.keys.PBKDF2_CONSTANTS['c'] = 100


log = get_logger('test.console_service')


@pytest.fixture
def test_app(request, tmpdir):

    class TestApp(EthApp):

        def start(self):
            super(TestApp, self).start()
            log.debug('adding test accounts')
            # high balance account
            self.services.accounts.add_account(Account.new('', tester.keys[0]), store=False)
            # low balance account
            self.services.accounts.add_account(Account.new('', tester.keys[1]), store=False)
            # locked account
            locked_account = Account.new('', tester.keys[2])
            locked_account.lock()
            self.services.accounts.add_account(locked_account, store=False)
            assert set(acct.address for acct in self.services.accounts) == set(tester.accounts[:3])

        def mine_next_block(self):
            """Mine until a valid nonce is found.

            :returns: the new head
            """
            log.debug('mining next block')
            block = self.services.chain.chain.head_candidate
            delta_nonce = 10**6
            for start_nonce in count(0, delta_nonce):
                bin_nonce, mixhash = mine(block.number, block.difficulty, block.mining_hash,
                                          start_nonce=start_nonce, rounds=delta_nonce)
                if bin_nonce:
                    break
            self.services.pow.recv_found_nonce(bin_nonce, mixhash, block.mining_hash)
            log.debug('block mined')
            assert self.services.chain.chain.head.difficulty == 1
            return self.services.chain.chain.head

    config = {
        'data_dir': str(tmpdir),
        'db': {'implementation': 'EphemDB'},
        'pow': {'activated': False},
        'p2p': {
            'min_peers': 0,
            'max_peers': 0,
            'listen_port': 29873
        },
        'node': {'privkey_hex': mk_random_privkey().encode('hex')},
        'discovery': {
            'boostrap_nodes': [],
            'listen_port': 29873
        },
        'eth': {
            'block': {  # reduced difficulty, increased gas limit, allocations to test accounts
                'GENESIS_DIFFICULTY': 1,
                'BLOCK_DIFF_FACTOR': 2,  # greater than difficulty, thus difficulty is constant
                'GENESIS_GAS_LIMIT': 3141592,
                'GENESIS_INITIAL_ALLOC': {
                    tester.accounts[0].encode('hex'): {'balance': 10**24},
                    tester.accounts[1].encode('hex'): {'balance': 1},
                    tester.accounts[2].encode('hex'): {'balance': 10**24},
                }
            }
        },
        'jsonrpc': {'listen_port': 29873}
    }
    services = [DBService, AccountsService, PeerManager, ChainService, PoWService, JSONRPCServer, Console]
    update_config_with_defaults(config, get_default_config([TestApp] + services))
    update_config_with_defaults(config, {'eth': {'block': ethereum.config.default_config}})
    app = TestApp(config)
    for service in services:
        service.register_with_app(app)

    def fin():
        log.debug('stopping test app')
        app.stop()
    request.addfinalizer(fin)

    log.debug('starting test app')
    app.start()
    return app


def test_send_transaction_with_contract(test_app):
    serpent_code = '''
def main(a,b):
    return(a ^ b)
'''
    tx_to = b''
    evm_code = serpent.compile(serpent_code)
    chain = test_app.services.chain.chain
    assert chain.head_candidate.get_balance(tx_to) == 0
    sender = test_app.services.accounts.unlocked_accounts[0].address
    assert chain.head_candidate.get_balance(sender) > 0

    eth = test_app.services.console.console_locals['eth']
    tx = eth.transact(to='', data=evm_code, startgas=500000, sender=sender)

    code = chain.head_candidate.account_to_dict(tx.creates)['code']
    assert len(code) > 2
    assert code != '0x'

    test_app.mine_next_block()

    creates = chain.head.get_transaction(0).creates
    code = chain.head.account_to_dict(creates)['code']
    assert len(code) > 2
    assert code != '0x'
