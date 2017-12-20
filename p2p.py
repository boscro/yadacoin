import socketio
import eventlet
import eventlet.wsgi
import json
import time
import signal
import sys
import requests
import base64
from multiprocessing import Process, Value, Array, Pool
from pymongo import MongoClient
from socketIO_client import SocketIO, BaseNamespace
from flask import Flask, render_template, request
from blockchainutils import BU
from blockchain import Blockchain, BlockChainException
from block import Block
from transaction import Transaction
from node import node


mongo_client = MongoClient('localhost')
db = mongo_client.yadacoin
collection = db.blocks
BU.collection = collection
Block.collection = collection
sio = socketio.Server()
app = Flask(__name__)


def output(string):
    sys.stdout.write(string)  # write the next character
    sys.stdout.flush()                # flush stdout buffer (actual character display)
    sys.stdout.write(''.join(['\b' for i in range(len(string))])) # erase the last written char

def signal_handler(signal, frame):
        print('Closing...')
        p.terminate()
        sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

def getblocks(sid):
    print("getblocks ")
    sio.emit('getblocksreply', data=[x for x in BU.get_blocks()], room=sid, namespace='/chat')
    print 'sent blocks!'

votes = {}
def newblock(sid, data):
    print("new block ", data)
    try:
        incoming_block = Block.from_dict(data)
        incoming_block.verify()
    except Exception as e:
        print "block is bad"
        print e
        return
    except BaseException as e:
        print "block is bad"
        print e
        return
    try:
        block = BU.get_block_by_index(incoming_block.index)
    except:
        return
    if block:
        # we have the same block. let the voting begin!
        try:
            if incoming_block.index not in votes:
                votes[incoming_block.index] = {}
            votes[incoming_block.index][incoming_block.signature] = 1
            sio.emit('getblockvote', data=incoming_block.to_dict(), skip_sid=sid, namespace='/chat')
        except:
            print 'there was a problem when initializing a vote on a new block'
    else:
        # dry run this block in the blockchain. Does it belong?
        try:
            blocks = BU.get_block_objs()
            blocks.append(incoming_block)
            blocks_sorted = sorted(blocks, key=lambda x: x.index)
            blockchain = Blockchain(blocks_sorted)
        except:
            print 'something went wrong with the blockchain dry run of new block'
        try:
            blockchain.verify()
            incoming_block.save()
        except Exception as e:
            print e
        except BaseException as e:
            print e

def newtransaction(sid, data):
    print("new transaction ", data)
    try:
        incoming_txn = Transaction.from_dict(data)
    except Exception as e:
        print "transaction is bad"
        print e
    except BaseException as e:
        print "transaction is bad"
        print e

    try:
        with open('miner_transactions.json') as f:
            data = json.loads(f.read())

        with open('miner_transactions.json', 'w') as f:
            abort = False
            for x in data:
                if x.get('id') == incoming_txn.transaction_signature:
                    abort = True
            if not abort:
                data.append(incoming_txn.to_dict())
                f.write(json.dumps(data, indent=4))

    except Exception as e:
        print e
    except BaseException as e:
        print e

def blockvotereply(sid, data):
    try:
        block = Block.from_dict(data)
        block.verify()
        if block.index not in votes:
            votes[block.index] = {}
        if block.signature not in votes[block.index]:
            votes[block.index][block.signature] = 0
        votes[block.index][block.signature] += 1

        peers = len(sio.manager.rooms['/chat'])

        if float(votes[block.index][block.signature]) / float(peers)  > 0.51:
            blocks = [x for x in BU.get_block_objs() if x.index != block.index]
            blocks.append(block)
            blocks_sorted = sorted(blocks, key=lambda x: x.index)
            blockchain = Blockchain(blocks_sorted)
            try:
                blockchain.verify()
                delete_block = Block.from_dict(BU.get_block_by_index(block.index))
                delete_block.delete()
                block.save()
            except:
                print 'incoming block does not belong here'

    except Exception as e:
        print e
    except BaseException as e:
        print e

def new_block_checker(current_index):
    while 1:
        try:
            current_index.value = BU.get_latest_block().get('index')
        except:
            pass
        time.sleep(1)

def get_peers(peers, config):
    connected = {}
    while 1:
        synced = False
        highest_height = 0
        block_heights = {}
        for peer in peers:
            try:
                res = requests.get('http://{peer}:8000/getblockheight'.format(peer=peer['ip']), timeout=1)
                height = int(json.loads(res.content).get('block_height'))
                block_heights[peer['ip']] = int(height)
            except:
                pass

        if block_heights:
            max_block_height = max([x for i, x in block_heights.items()])
            peers_with_longest_chain = [i for i, x in block_heights.items() if x == max_block_height]

            latest_block_local = Block.from_dict(BU.get_latest_block())
            previous_block = Block.from_dict(BU.get_block_by_index(latest_block_local.index - 1))
            if int(latest_block_local.index) > max_block_height:
                output("I have the longest blockchain. I'm the shining example.")
                continue

            if max_block_height - int(latest_block_local.index) > 1:
                output("MASSIVELY OUT OF SYNC!!! RESTART NODE!!!")
                continue

            blocks = {}
            if int(latest_block_local.index) == max_block_height:
                # i'm one of the top dogs, count me in
                blocks[latest_block_local.signature] = []
                blocks[latest_block_local.signature].append(latest_block_local)
                count_me_in = True
            else:
                count_me_in = False

            for peer in peers_with_longest_chain:
                #take a vote, gather blocks for this height from all peers
                try:
                    res = requests.get('http://{peer}:8000/getblock?index={index}'.format(peer=peer, index=max_block_height), timeout=1)
                    inbound_block = json.loads(res.content)
                    block = Block.from_dict(inbound_block)
                    block.verify()
                    if count_me_in:
                        if block.prev_hash != previous_block.hash:
                            # not a valid next block
                            continue
                    else:
                        if block.prev_hash != latest_block_local.hash:
                            # I'm probably way behind
                            continue
                    if block.signature not in blocks:
                        blocks[block.signature] = []
                    blocks[block.signature].append(block)
                except:
                    pass
                
            highest_sig_count = max([len(x) for i, x in blocks.items()])
            if len([len(x) for i, x in blocks.items() if len(x) == highest_sig_count]) > 1:
                # if there's a tie, pick a winner by getting the lowest value signature
                min_sig = base64.b64encode(min([base64.b64decode(x) for x in blocks.keys()]))
                winning_block = blocks[min_sig][0]
            else:
                winning_block = [x for i, x in blocks.items() if len(x) == highest_sig_count][0]

            BU.collection.remove({"index": winning_block.index})
            BU.collection.insert(winning_block.to_dict())
        else:
            # start the competition again
            node(config)

class ChatNamespace(BaseNamespace):
    def on_connect(self):
        print 'client connected!'
    
    def on_getblocksreply(self, *args):
        print 'getblocksreply'
        try:
            blocks = []
            for block_dict in args[0]:
                block = Block.from_dict(block_dict)
                block.verify()
                blocks.append(block)

            blocks_sorted = sorted(blocks, key=lambda x: x.index)
            if len(BU.get_latest_block()):
                biggest_index = BU.get_latest_block().get('index')
            else:
                biggest_index = -1
            if blocks_sorted:
                biggest_index_incoming = blocks_sorted[-1].index
            else:
                biggest_index_incoming = -1
            if blocks_sorted and biggest_index < biggest_index_incoming:
                blockchain = Blockchain(blocks_sorted)
                try:
                    blockchain.verify()
                except:
                    print 'peer blockchain did not verify, aborting update'
                    return
                collection.remove({})
                print 'truncating!'
                for block in blocks_sorted:
                    block.verify()
                    block.save()
                    print 'saving!'
            else:
                print 'my chain is longer!', biggest_index, biggest_index_incoming
                return
            print 'on_getblocksreply', 'done!'
        except Exception as e:
            print e
        except BaseException as e:
            print e

    def on_error(self, error):
        print error

    def on_message(self, message):
        print message

@app.route('/getblocks')
def app_getblocks():
    return json.dumps([x for x in BU.get_blocks()])

@app.route('/getblockheight')
def app_getblockheight():
    return json.dumps({'block_height': BU.get_latest_block().get('index')})

@app.route('/getblock')
def app_getblock():
    idx = int(request.args.get('index'))
    return json.dumps(BU.get_block_by_index(idx))

@sio.on('custom', namespace='/chat')
def custom(sid):
    print("custom hahahahaha ")

@sio.on('connect', namespace='/chat')
def connect(sid, environ):
    print("connect ", sid)

@sio.on('newblock', namespace='/chat')
def sio_newblock(sid, data):
    newblock(sid, data)

@sio.on('newtransaction', namespace='/chat')
def sio_newtransaction(sid, data):
    newtransaction(sid, data)

@sio.on('getblocksreply', namespace='/chat')
def sio_getblocksreply(sid, data):
    getblocksreply(sid, data)

@sio.on('blockvotereply', namespace='/chat')
def sio_blockvotereply(sid, data):
    blockvotereply(sid, data)

@sio.on('getblocks', namespace='/chat')
def sio_getblocks(sid):
    getblocks(sid)

if __name__ == '__main__':
    with open('config.json') as f:
        config = json.loads(f.read())

    with open('peers.json') as f:
        peers = json.loads(f.read())

    p = Process(target=get_peers, args=(peers, config))
    p.start()
    # wrap Flask application with engineio's middleware
    app = socketio.Middleware(sio, app)

    # deploy as an eventlet WSGI server
    eventlet.wsgi.server(eventlet.listen(('', 8000)), app)
