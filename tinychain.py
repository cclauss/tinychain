#!/usr/bin/env python3
"""
tinychain

Usage:
  tinychain serve
  tinychain send <addr> <amount>

Options:
  -h --help   Show this screen.
  --version   Show version.

Notes:

- Where sensible, I've used naming which corresponds to bitcoin codebase
  equivalents. This breaks with Python convention but hopefully it makes for
  easier greping through bitcoin should you get curious.

Unrealistic simplifications:

- Byte encoding and endianness are very important when serializing a
  data structure to be hashed in Bitcoin and are not reproduced
  faithfully here. In fact, serialization of any kind here is slipshod and
  in many cases relies on implicit expectations about Python builtin
  __repr__ methods.
  See: https://en.bitcoin.it/wiki/Protocol_documentation

- Block `bit` targets are considerably simplified here.
  See: https://bitcoin.org/en/developer-reference#target-nbits

- No UTXO set.

- Transaction types limited to P2PKH.

- Initial Block Download eschews `getdata` and instead returns block payloads
  directly in `inv`.

Some shorthand:

- PoW: proof of work
- Tx: transaction

Resources:

- https://en.bitcoin.it/wiki/Protocol_rules

TODO:

- reorg utxo maintenance
- write chain to disk
- keep the mempool heap sorted
- deal with orphan blocks
- replace-by-fee

"""
import binascii
import time
import json
import hashlib
import threading
import logging
import socketserver
import socket
import random
import os
from functools import lru_cache, wraps
from typing import (
    Iterable, NamedTuple, Dict, Mapping, Union, get_type_hints, Tuple,
    Callable)

import ecdsa
from docopt import docopt
from base58 import b58encode_check


logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s][%(module)s:%(lineno)d] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


class Params:
    MAX_BLOCK_SERIALIZED_SIZE = 1000000  # bytes = 1MB

    # Coinbase transaction outputs can be spent after this many blocks have
    # elapsed since being mined.
    # XXX is "100" in core.
    #
    COINBASE_MATURITY = 2

    # Accept blocks which timestamped as being from the future up to this
    # amount.
    MAX_FUTURE_BLOCK_TIME = (60 * 60 * 2)

    # The number of Belushis per coin.
    #
    # #bitcoin-name: COIN
    BELUSHIS_PER_COIN = int(100e6)

    TOTAL_COINS = 21_000_000

    # The maximum number of Belushis that will ever be found.
    MAX_MONEY = BELUSHIS_PER_COIN * TOTAL_COINS

    # The duration we want to pass between blocks being found, in seconds.
    # This is lower than Bitcoin's configuation (10 * 60).
    #
    # #bitcoin-name: PowTargetSpacing
    TIME_BETWEEN_BLOCKS_IN_SECS_TARGET = 1 * 60

    # The number of seconds we want a difficulty period to last.
    #
    # Note that this differs considerably from the behavior in Bitcoin, which
    # is configured to target difficulty periods of (10 * 2016) minutes.
    #
    # #bitcoin-name: PowTargetTimespan
    DIFFICULTY_PERIOD_IN_SECS_TARGET = (60 * 60 * 10)

    # After this number of blocks are found, adjust difficulty.
    #
    # #bitcoin-name DifficultyAdjustmentInterval
    DIFFICULTY_PERIOD_IN_BLOCKS = (
        DIFFICULTY_PERIOD_IN_SECS_TARGET / TIME_BETWEEN_BLOCKS_IN_SECS_TARGET)

    INITIAL_DIFFICULTY_BITS = 22

    # The number of blocks after which the mining subsidy will halve.
    #
    # #bitcoin-name: SubsidyHalvingInterval
    HALVE_SUBSIDY_AFTER_BLOCKS_NUM = 210_000


# Used to represent the specific output (since a transaction can have many
# outputs) within a transaction.
OutPoint = NamedTuple('OutPoint', [('txid', str), ('txout_idx', int)])


class TxIn(NamedTuple):
    """Inputs to a Transaction."""
    # A reference to the output we're spending. This is None for coinbase
    # transactions.
    to_spend: Union[OutPoint, None]

    # The (signature, pubkey) pair which unlocks the TxOut for spending.
    unlock_sig: bytes
    unlock_pk: bytes

    # A sender-defined sequence number which allows us replacement of the txn
    # if desired.
    sequence: int


class TxOut(NamedTuple):
    """Outputs from a Transaction."""
    # The number of Belushis this awards.
    value: int

    # The public key of the owner of this Txn.
    to_address: str


class UnspentTxOut(NamedTuple):
    value: int
    to_address: str

    # The ID of the transaction this output belongs to.
    txid: str
    tx_idx: int

    # Did this TxOut from from a coinbase transaction?
    is_coinbase: bool

    # The blockchain height this TxOut was included in the chain.
    height: int

    @classmethod
    def from_mempool_txn(cls, txn) -> Iterable['UnspentTxOut']:
        # UTXO contained in mempool can't be a coinbase transaction --
        # otherwise it would have been mined and thus found in `utxo_set`.
        return [
            cls(**i, txid=txn.id, is_coinbase=False, height=-1)
            for i in txn.txouts]

    @property
    def outpoint(self):
        return OutPoint(self.txid, self.tx_idx)


class Transaction(NamedTuple):
    txins: Iterable[TxIn]
    txouts: Iterable[TxOut]

    # The block number or timestamp at which this transaction is unlocked.
    # < 500000000: Block number at which this transaction is unlocked.
    # >= 500000000: UNIX timestamp at which this transaction is unlocked.
    locktime: int = None

    @property
    def is_coinbase(self) -> bool:
        return len(self.txins) == 1 and self.txins[0].to_spend is None

    @classmethod
    def create_coinbase(cls, pay_to_addr, value, height):
        return cls(
            txins=[TxIn(
                to_spend=None,
                # Push current block height into unlock_sig so that this
                # transaction's ID is unique relative to other coinbase txns.
                unlock_sig=str(height).encode(),
                unlock_pk=None,
                sequence=0)],
            txouts=[TxOut(
                value=value,
                to_address=pay_to_addr)],
        )

    @property
    def id(self) -> str:
        return sha256d(serialize(self))

    def validate_basics(self, as_coinbase=False):
        if (not self.txouts) or (not self.txins and not as_coinbase):
            raise TxnValidationError('Missing txouts or txins')

        if len(serialize(self)) > Params.MAX_BLOCK_SERIALIZED_SIZE:
            raise TxnValidationError('Too large')

        if sum(t.value for t in self.txouts) > Params.MAX_MONEY:
            raise TxnValidationError('Spend value too high')


class Block(NamedTuple):
    # A version integer.
    version: int

    # A hash of the previous block's header.
    prev_block_hash: str

    # A hash of the Merkle tree containing all txns.
    merkle_hash: str

    # A UNIX timestamp of when this block was created.
    timestamp: int

    # The difficulty target; i.e. the hash of this block header must be under
    # this value to consider work proved.
    bits: int

    # The value that's incremented in an attempt to get the block header to
    # hash to a value below `bits`.
    nonce: int

    txns: Iterable[Transaction]

    def header(self, nonce=None) -> str:
        return (
            f'{self.version}{self.prev_block_hash}{self.merkle_hash}'
            f'{self.timestamp}{self.bits}{nonce or self.nonce}')

    @property
    def id(self) -> str:
        return sha256d(self.header())


# Chain
# ----------------------------------------------------------------------------

genesis_block = Block(
    version=0,
    prev_block_hash=None,
    merkle_hash=(
        'dfef8eb972026bbe9e98b26616fe90e60e3ff223d0a596e78bde6632109d7ef0'),
    timestamp=1501396299,
    bits=26,
    nonce=1845989,
    txns=[Transaction(
        txins=[],
        txouts=[TxOut(
            value=5000000000,
            to_address='143UVyz7ooiAv1pMqbwPPpnH4BV9ifJGFF')],
        locktime=None)])

# The highest proof-of-work, valid blockchain.
#
# #bitcoin-name: chainActive
active_chain: Iterable[Block] = []

# Branches off of the main chain.
side_branches: Iterable[Iterable[Block]] = []

# Synchronize access to the active chain and side branches.
chain_lock = threading.RLock()


def with_lock(lock):
    def dec(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with lock:
                return func(*args, **kwargs)
        return wrapper
    return dec


orphan_blocks: Iterable[Block] = []

# Used to signify the active chain in `find_block`.
ACTIVE_CHAIN_IDX = 0


@with_lock(chain_lock)
def get_current_height():
    return len(active_chain)


def idx_to_chain(idx):
    return active_chain if idx == ACTIVE_CHAIN_IDX else side_branches[idx - 1]


@with_lock(chain_lock)
def txn_iterator(chain):
    return (
        (txn, block, height)
        for height, block in enumerate(chain) for txn in block.txns)


def txin_iterator(chain):
    return (
        (txin, height)
        for txn, _, height in txn_iterator(chain) for txin in txn.txins)


@with_lock(chain_lock)
def get_height(block_hash: str) -> int:
    for i, block in enumerate(active_chain[::-1]):
        if block.id == block_hash:
            return i
    return -1


@with_lock(chain_lock)
def find_block(block_hash: str, chain=active_chain) -> (Block, int, int):
    chains = [chain] if chain else [active_chain, *side_branches]

    for chain_idx, chain in enumerate(chains):
        for height, block in enumerate(chain):
            if block.id == block_hash:
                return (block, height, chain_idx)
    return (None, None, None)


utxo_set: Mapping[OutPoint, UnspentTxOut] = {}


def add_to_utxo(txout, tx, idx, is_coinbase, height):
    utxo = UnspentTxOut(
        *txout, txid=tx.id, tx_idx=idx, is_coinbase=is_coinbase, height=height)
    utxo_set[utxo.outpoint] = utxo


def rm_from_utxo(txid, tx_idx):
    del utxo_set[OutPoint(txid, tx_idx)]


@with_lock(chain_lock)
def reorg_if_necessary() -> int:
    reorged = False

    # TODO should probably be using `chainwork` for the basis of
    # comparison here.
    for i, chain in enumerate(side_branches, 1):
        fork_block, fork_idx, _ = find_block(chain[0].prev_block_hash)
        active_height = len(active_chain)
        branch_height = len(chain) + fork_idx

        if branch_height > active_height:
            reorged |= try_reorg(chain)

    return reorged


@with_lock(chain_lock)
def try_reorg(branch, branch_idx, fork_idx) -> bool:
    # Use the global keyword so that we can actually swap out the reference
    # in case of a reorg.
    global active_chain
    global side_branches

    old_active_branch = active_chain[(fork_idx + 1):]
    active_chain = active_chain[:fork_idx] + branch

    # Keep track of the changes we need to make to the mempool if this reorg
    # succeeds.
    mempool_mods = {
        'add': {txn.id: txn for block in branch for txn in block.txns},
        'rm': [txn.id for block in old_active_branch for txn in block.txns],
    }

    for block in branch:
        try:
            validate_block(block)
        except BlockValidationError:
            logger.info("block reorg failed - block %s invalid", block.id)
            active_chain = active_chain[:fork_idx] + old_active_branch
            return False
        else:
            active_chain.append(block)

    # Fix up side branches: remove new active, add old active.
    side_branches.pop(branch_idx)
    side_branches.append(old_active_branch)

    for txn_id in mempool_mods['rm']:
        mempool.pop(txn_id)

    for txn_id, txn in mempool_mods['add'].items():
        mempool[txn_id] = txn

    logger.info(
        'chain reorg! New height: %s, tip: %s',
        len(active_chain), active_chain[-1].id)

    return True


def get_median_time_past(num_last_blocks: int) -> int:
    """Grep for: GetMedianTimePast."""
    last_n_blocks = active_chain[::-1][:num_last_blocks]

    if not last_n_blocks:
        return 0

    return last_n_blocks[len(last_n_blocks) // 2].timestamp


# Proof of work
# ----------------------------------------------------------------------------

def get_next_work_required(prev_block_hash: str) -> int:
    if not prev_block_hash:
        return Params.INITIAL_DIFFICULTY_BITS

    (prev_block, prev_height, _) = find_block(prev_block_hash, chain=None)

    if (prev_height + 1) % Params.DIFFICULTY_PERIOD_IN_BLOCKS != 0:
        return prev_block.bits

    with chain_lock:
        # #bitcoin-name: CalculateNextWorkRequired
        period_start_block = active_chain[max(
            prev_height - (Params.DIFFICULTY_PERIOD_IN_BLOCKS - 1), 0)]

    actual_time_taken = prev_block.timestamp - period_start_block.timestamp

    if actual_time_taken < Params.DIFFICULTY_PERIOD_IN_SECS_TARGET:
        # Increase the difficulty
        return prev_block.bits + 1
    elif actual_time_taken > Params.DIFFICULTY_PERIOD_IN_SECS_TARGET:
        return prev_block.bits - 1
    else:
        # Wow, that's unlikely.
        return prev_block.bits


def assemble_and_solve_block(pay_coinbase_to_addr, txns=None):
    with chain_lock:
        prev_block_hash = active_chain[-1].id if active_chain else None

    block = Block(
        version=0,
        prev_block_hash=prev_block_hash,
        merkle_hash='',
        timestamp=int(time.time()),
        bits=get_next_work_required(prev_block_hash),
        nonce=0,
        txns=txns or [],
    )

    if not block.txns:
        block = select_from_mempool(block)

    fees = calculate_fees(block)
    coinbase_txn = Transaction.create_coinbase(
        my_address, (get_block_subsidy() + fees), len(active_chain))
    block = block._replace(txns=[coinbase_txn, *block.txns])
    block = block._replace(merkle_hash=get_merkle_root_of_txns(block.txns).val)

    if len(serialize(block)) > Params.MAX_BLOCK_SERIALIZED_SIZE:
        raise ValueError('txns specified create a block too large')

    return mine(block)


def calculate_fees(block):
    fee = 0

    def utxo_from_block(txin):
        tx = [t.txouts for t in block.txns if t.id == txin.to_spend.txid]
        return tx[0][txin.to_spend.tx_idx] if tx else None

    def find_utxo(txin):
        return utxo_set.get(txin.to_spend) or utxo_from_block(txin)

    for txn in block.txns:
        spent = sum(find_utxo(i).value for i in txn.txins)
        sent = sum(o.value for o in txn.txouts)
        fee += (spent - sent)

    return fee


def get_block_subsidy() -> int:
    halvings = len(active_chain) // Params.HALVE_SUBSIDY_AFTER_BLOCKS_NUM

    if halvings >= 64:
        return 0

    return 50 * Params.BELUSHIS_PER_COIN // (2 ** halvings)


# Signal to communicate to the mining thread that it should stop mining because
# we've updated the chain with a new block.
mine_interrupt = threading.Event()


def mine(block):
    start = time.time()
    nonce = 0
    target = (1 << (256 - block.bits))
    mine_interrupt.clear()

    while int(sha256d(block.header(nonce)), 16) >= target:
        nonce += 1

        if nonce % 10000 == 0 and mine_interrupt.is_set():
            logger.info('[mining] interrupted')
            mine_interrupt.clear()
            return None

    block = block._replace(nonce=nonce)
    duration = int(time.time() - start) or 0.001
    khs = (block.nonce // duration) // 1000
    logger.info(f'block found! {duration} s - {khs} KH/s - {block.id}')

    return block


def mine_forever():
    while True:
        block = assemble_and_solve_block(my_address)

        if block:
            connect_block(block)


# Validation
# ----------------------------------------------------------------------------


def validate_txn(txn: Union[Transaction, str],
                 as_coinbase: bool = False,
                 allow_utxo_from_mempool: bool = True) -> Transaction:
    if not isinstance(txn, Transaction):
        try:
            txn = deserialize(txn)
        except Exception:
            logger.exception(f"Couldn't deserialize transaction {txn}")
            raise TxnValidationError('Could not deserialize')

    txn.validate_basics(as_coinbase=as_coinbase)

    available_to_spend = 0

    for i, txin in enumerate(txn.txins):
        utxo = utxo_set.get(txin.to_spend)

        if allow_utxo_from_mempool:
            utxo = utxo or find_utxo_in_mempool(txin)

        if not utxo:
            raise TxnValidationError(
                f'Could find no UTXO for TxIn[{i}] -- orphaning txn',
                to_orphan=txn)

        if utxo.is_coinbase and \
                (get_current_height() - utxo.height) < \
                Params.COINBASE_MATURITY:
            raise TxnValidationError(f'Coinbase UTXO not ready for spend')

        try:
            validate_signature_for_spend(txin, utxo, txn)
        except TxUnlockError:
            raise TxnValidationError(f'{txin} is not a valid spend of {utxo}')

        available_to_spend += utxo.value

    if available_to_spend < sum(o.value for o in txn.txouts):
        raise TxnValidationError('Spend value is more than available')

    return txn


def validate_signature_for_spend(txin, utxo: UnspentTxOut, txn):
    pubkey_as_addr = pubkey_to_address(txin.unlock_pk)
    verifying_key = ecdsa.VerifyingKey.from_string(
        txin.unlock_pk, curve=ecdsa.SECP256k1)

    if pubkey_as_addr != utxo.to_address:
        raise TxUnlockError("Pubkey doesn't match")

    try:
        spend_msg = build_spend_message(
            txin.to_spend, txin.unlock_pk, txin.sequence, txn.txouts)
        verifying_key.verify(txin.unlock_sig, spend_msg)
    except Exception:
        logger.exception('Key verification failed')
        raise TxUnlockError("Signature doesn't match")

    return True


def build_spend_message(to_spend, pk, sequence, txouts) -> bytes:
    # TODO Double check that this is ~roughly~ equivalent to SIGHASH_ALL.
    return sha256d(
        serialize(to_spend) + str(sequence) +
        binascii.hexlify(pk).decode() + serialize(txouts)).encode()


@with_lock(chain_lock)
def validate_block(block: Union[Block, str]) -> Block:
    if not isinstance(block, Block):
        try:
            block = deserialize(block)
        except Exception:
            logger.exception(f"Couldn't deserialize block {block}")
            raise BlockValidationError("Couldn't deserialize")

    if not block.txns:
        raise BlockValidationError('txns empty')

    if block.timestamp - time.time() > Params.MAX_FUTURE_BLOCK_TIME:
        raise BlockValidationError('Block timestamp too far in future')

    if int(block.id, 16) > (1 << (256 - block.bits)):
        raise BlockValidationError("Block header doesn't satisfy bits")

    if [i for (i, tx) in enumerate(block.txns) if tx.is_coinbase] != [0]:
        raise BlockValidationError('First txn must be coinbase and no more')

    try:
        for i, txn in enumerate(block.txns):
            txn.validate_basics(as_coinbase=(i == 0))
    except TxnValidationError:
        logger.exception(f"Transaction {txn} in {block} failed to validate")
        raise BlockValidationError('Invalid txn {txn.id}')

    if get_merkle_root_of_txns(block.txns).val != block.merkle_hash:
        raise BlockValidationError('Merkle hash invalid')

    if get_next_work_required(block.prev_block_hash) != block.bits:
        raise BlockValidationError('bits is incorrect')

    if block.timestamp <= get_median_time_past(11):
        raise BlockValidationError('timestamp too old')

    if not block.prev_block_hash and not active_chain:
        # This is the genesis block.
        prev_block_chain_idx = ACTIVE_CHAIN_IDX
    else:
        prev_block, prev_block_height, prev_block_chain_idx = find_block(
            block.prev_block_hash, chain=None)

        if not prev_block:
            raise BlockValidationError(
                f'prev block {block.prev_block_hash} not found in any chain',
                to_orphan=block)

        # No more validation for a block getting attached to a branch.
        if prev_block_chain_idx != ACTIVE_CHAIN_IDX:
            return block, prev_block_chain_idx

        # Prev. block found in active chain, but isn't tip => new fork.
        elif prev_block != active_chain[-1]:
            return block, prev_block_chain_idx + 1  # Non-existent

    for txn in block.txns[1:]:
        try:
            validate_txn(txn, allow_utxo_from_mempool=False)
        except TxnValidationError:
            msg = f"{txn} failed to validate"
            logger.exception(msg)
            raise BlockValidationError(msg)

    return block, prev_block_chain_idx


class BaseException(Exception):
    def __init__(self, msg):
        self.msg = msg


class TxUnlockError(BaseException):
    pass


class TxnValidationError(BaseException):
    def __init__(self, *args, to_orphan: Transaction = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.to_orphan = to_orphan


class BlockValidationError(BaseException):
    def __init__(self, *args, to_orphan: Block = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.to_orphan = to_orphan


# mempool
# ----------------------------------------------------------------------------

# Set of yet-unmined transactions.
mempool: Dict[str, Transaction] = {}

# Set of orphaned (i.e. has inputs referencing yet non-existent UTXOs)
# transactions.
orphan_txns: Iterable[Transaction] = []


def find_with_txin_in_mempool(txin):
    for txn in mempool.values():
        if txin in txn.txins:
            return txn
    return None


def find_utxo_in_mempool(txin) -> UnspentTxOut:
    tx_to_spend_hash = txin.to_spend.txid
    idx = txin.to_spend.txout_idx
    found_in_mempool = mempool.get(tx_to_spend_hash)

    if found_in_mempool:
        found_utxos = UnspentTxOut.from_mempool_txn(found_in_mempool.id)
    else:
        logger.debug("Couldn't find txn %s", tx_to_spend_hash)
        return None

    try:
        return found_utxos[idx]
    except IndexError:
        logger.debug(
            "Transaction %s does not have an output at index %s",
            tx_to_spend_hash, idx)
        return None


def select_from_mempool(block: Block) -> Block:
    """Fill a Block with transactions from the mempool."""
    added_to_block = set()

    def check_block_size(b) -> bool:
        return len(serialize(block)) < Params.MAX_BLOCK_SERIALIZED_SIZE

    def add_to_block(block, txid) -> Block:
        if txid in added_to_block:
            return block

        tx = mempool[txid]

        # For any txin that can't be found in the main chain, find its
        # transaction in the mempool (if it exists) and add it to the block.
        for txin in tx.txins:
            if txin.to_spend in utxo_set:
                continue

            in_mempool = find_utxo_in_mempool(txin)

            if not in_mempool:
                logger.debug(f"Couldn't find UTXO for {txin}")
                return None

            block = add_to_block(in_mempool.txid)
            if not block:
                logger.debug(f"Couldn't add parent")
                return None

        newblock = block._replace(txns=[*block.txns, tx])

        if check_block_size(newblock):
            logger.debug(f'added tx {tx.id} to block')
            added_to_block.add(txid)
            return newblock
        else:
            return block

    for txid in mempool:
        newblock = add_to_block(block, txid)

        if check_block_size(newblock):
            block = newblock
        else:
            break

    return block


# Merkle trees
# ----------------------------------------------------------------------------

class MerkleNode(NamedTuple):
    val: str
    children: Iterable = None


def get_merkle_root_of_txns(txns):
    return get_merkle_root(*[t.id for t in txns])


@lru_cache(maxsize=1024)
def get_merkle_root(*leaves: Tuple[str]) -> MerkleNode:
    """Builds a Merkle tree and returns the root given some leaf values."""
    if len(leaves) % 2 == 1:
        leaves = leaves + (leaves[-1],)

    def find_root(nodes):
        newlevel = [
            MerkleNode(sha256d(i1.val + i2.val), children=[i1, i2])
            for [i1, i2] in _chunks(nodes, 2)
        ]

        return find_root(newlevel) if len(newlevel) > 1 else newlevel[0]

    return find_root([MerkleNode(sha256d(l)) for l in leaves])


# Peer-to-peer
# ----------------------------------------------------------------------------

peerlist = [p for p in os.environ.get('TC_PEERLIST', '').split(',') if p]


def accept_txn(serialized_txn: str):
    try:
        txn = validate_txn(serialized_txn)
    except TxnValidationError as e:
        if e.to_orphan:
            logger.info(f'txn {e.to_orphan.id} submitted as orphan')
            orphan_txns.append(e.to_orphan)
        else:
            logger.exception(f'txn rejected')
    else:
        logger.info(f'txn {txn.id} added to mempool')
        mempool[txn.id] = txn

        for peer in peerlist:
            send_to_peer(txn, peer)


def connect_block(block: Union[str, Block]) -> Union[None, Block]:
    """Accept a block and return the chain index we append it to."""
    try:
        block, chain_idx = validate_block(block)
    except BlockValidationError as e:
        logger.exception('block %s failed validation', block.id)
        if e.to_orphan:
            logger.info(f"saw orphan block {block.id}")
            orphan_blocks.append(e.to_orphan)
        return None

    if find_block(block.id)[0]:  # Already seen it.
        return None

    logger.info(f'connecting block {block.id} to chain {chain_idx}')
    chain = idx_to_chain(chain_idx)
    chain.append(block)

    # Remove txs from mempool
    for tx in block.txns:
        mempool.pop(tx.id, None)

        if not tx.is_coinbase:
            for txin in tx.txins:
                rm_from_utxo(*txin.to_spend)
        for i, txout in enumerate(tx.txouts):
            add_to_utxo(txout, tx, i, tx.is_coinbase, len(chain))

    if reorg_if_necessary() or chain_idx == ACTIVE_CHAIN_IDX:
        mine_interrupt.set()
        logger.info(
            f'block accepted '
            f'height={len(active_chain) - 1} txns={len(block.txns)}')

    for peer in peerlist:
        send_to_peer(block, peer)

    return chain_idx


@with_lock(chain_lock)
def disconnect_block(block, chain=active_chain):
    assert block == chain[-1], "Block being disconnected must be tip."

    for tx in block.txns:
        mempool[tx.id] = tx

        for txin in tx.txins:
            add_to_utxo(*_find_txout_for_txin(txin, chain))
        for i in range(tx.txouts):
            rm_from_utxo(tx.id, i)

    chain.pop()
    logger.info(f'block {block.id} disconnected')


def _find_txout_for_txin(txin, chain):
    """TODO: clean this garbage up."""
    txid, tx_idx = txin.to_spend

    for tx, block, height in txn_iterator(chain):
        if tx.id == txid:
            txout = tx.txouts[tx_idx]
            return (txout, tx, tx_idx, tx.is_coinbase, height)


class GetBlocks(NamedTuple):
    """
    See https://bitcoin.org/en/developer-guide#blocks-first

    """
    from_blockid: str

    def handle(self, sock, peername):
        CHUNK_SIZE = 50
        logger.debug("[p2p] recv getblocks from {peername[0]}")

        with chain_lock:
            # Only reference our current active chain.
            _, height, _ = find_block(self.from_blockid)

        # If we don't recognize the requested hash as part of the active
        # chain, start at the genesis block.
        height = height or 1

        with chain_lock:
            blocks = active_chain[height:(height + CHUNK_SIZE)]

        send_to_peer(Inv('block', blocks))


class Inv(NamedTuple):
    type: Union['block', 'tx']
    payload: Iterable[str]

    def handle(self, sock, peername):
        logger.debug("[p2p] recv inv from {peername[0]}")

        if self.type == 'block':
            not_in_chain = [
                b for b in self.payload if not find_block(b, chain=None)[0]]

            if not not_in_chain:
                return

            for block in not_in_chain:
                connect_block(block)

            with chain_lock:
                send_to_peer(GetBlocks(active_chain[-1].id))

        if self.type == 'tx':
            for tx in (t for t in self.payload if t not in mempool):
                mempool[tx.id] = tx


def find_utxos_for_address(addr):
    return [utxo for utxo in utxo_set.values() if utxo.to_address == addr]


class Balance(NamedTuple):
    addr: str

    def handle(self, sock, peername):
        my_coins = find_utxos_for_address(self.addr)
        sock.sendall(str(sum(i.value for i in my_coins)).encode())


class Send(NamedTuple):
    addr: str
    value: int

    def handle(self, sock, peername):
        selected = set()
        my_coins = list(sorted(
            find_utxos_for_address(my_address),
            key=lambda i: (i.value, i.height)))

        for coin in my_coins:
            selected.add(coin)
            if sum(i.value for i in selected) > self.value:
                break

        txout = TxOut(value=self.value, to_address=self.addr)

        def make_txin(coin):
            sequence = 0
            pk = verifying_key.to_string()
            spend_msg = build_spend_message(
                coin.outpoint, pk, sequence, [txout])

            return TxIn(
                to_spend=coin.outpoint,
                unlock_pk=pk,
                unlock_sig=signing_key.sign(spend_msg),
                sequence=sequence,
            )

        txn = Transaction(
            txins=[make_txin(coin) for coin in selected],
            txouts=[txout])

        logger.info(f'submitting to network: {txn}')
        accept_txn(txn)


class GetMempool(NamedTuple):
    def handle(self, sock, peername):
        sock.sendall(serialize(list(mempool.keys())).encode())


def read_all_from_socket(req) -> object:
    data = b''
    while True:
        got = req.recv(1024)
        if not got:
            break
        data += got
    return deserialize(data.decode())


def send_to_peer(data, peer=None):
    peer = peer or random.choice(peerlist)

    try:
        with socket.create_connection(peer.split(':')) as s:
            s.sendall(serialize(data).encode())
    except Exception:
        logger.exception(f'failed to send to peer {peer[0]}')
        return False
    return True


class TCPHandler(socketserver.BaseRequestHandler):

    def handle(self):
        data = read_all_from_socket(self.request)

        if hasattr(data, 'handle') and isinstance(data.handle, Callable):
            data.handle(self.request, self.request.getpeername())
        elif isinstance(data, Transaction):
            accept_txn(data)
        elif isinstance(data, Block):
            connect_block(data)


# Wallet
# ----------------------------------------------------------------------------

def pubkey_to_address(pubkey: bytes) -> str:
    if 'ripemd160' not in hashlib.algorithms_available:
        raise RuntimeError('missing ripemd160 hash algorithm')

    sha = hashlib.sha256(pubkey).digest()
    ripe = hashlib.new('ripemd160', sha).digest()
    return b58encode_check(b'\x00' + ripe)


def new_signing_key() -> ecdsa.SigningKey:
    return ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)


def get_signing_key(privkey: bytes) -> ecdsa.SigningKey:
    return ecdsa.SigningKey.from_string(privkey, curve=ecdsa.SECP256k1)


def write_wallet_file(privkey):
    with open('wallet.dat', 'wb') as f:
        f.write(privkey)


try:
    with open('wallet.dat', 'rb') as f:
        signing_key = get_signing_key(f.read())
except Exception:
    logger.exception("generating new signing key")
    signing_key = new_signing_key()
    write_wallet_file(signing_key.to_string())


verifying_key = signing_key.get_verifying_key()
my_address = pubkey_to_address(verifying_key.to_string())
logger.info(f"your address is {my_address}")


# Uninteresting utilities
# ----------------------------------------------------------------------------

def serialize(obj) -> str:
    """NamedTuple-flavored serialization to JSON."""
    def contents_to_primitive(o):
        if hasattr(o, '_asdict'):
            o = {**o._asdict(), '_type': type(o).__name__}
        elif isinstance(o, list):
            return [contents_to_primitive(i) for i in o]
        elif isinstance(o, bytes):
            return binascii.hexlify(o).decode()
        elif not isinstance(o, (dict, bytes, str, int, type(None))):
            raise ValueError(f"Can't serialize {o}")

        if isinstance(o, Mapping):
            for k, v in o.items():
                o[k] = contents_to_primitive(v)

        return o

    return json.dumps(
        contents_to_primitive(obj), sort_keys=True, separators=(',', ':'))


def deserialize(serialized: str) -> object:
    """NamedTuple-flavored serialization from JSON."""
    gs = globals()

    def contents_to_objs(o):
        if isinstance(o, list):
            return [contents_to_objs(i) for i in o]
        elif not isinstance(o, Mapping):
            return o

        _type = gs[o.pop('_type', None)]
        bytes_keys = {
            k for k, v in get_type_hints(_type).items() if v == bytes}

        for k, v in o.items():
            o[k] = contents_to_objs(v)

            if k in bytes_keys:
                o[k] = binascii.unhexlify(o[k])

        return _type(**o)

    return contents_to_objs(json.loads(serialized))


def sha256d(s: Union[str, bytes]) -> str:
    """A double SHA-256 hash."""
    if not isinstance(s, bytes):
        s = s.encode()

    return hashlib.sha256(hashlib.sha256(s).digest()).hexdigest()


def _chunks(l, n) -> Iterable[Iterable]:
    return (l[i:i + n] for i in range(0, len(l), n))


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


def main(args: dict):
    if args['serve']:
        workers = []
        server = ThreadedTCPServer(('0.0.0.0', 9999), TCPHandler)

        for fnc in (mine_forever, server.serve_forever):
            worker = threading.Thread(target=fnc)
            worker.daemon = True
            worker.start()
            workers.append(worker)

        logger.info('[p2p] listening on 9999')
        [w.join() for w in workers]


if __name__ == '__main__':
    main(docopt(__doc__, version='0.1'))
