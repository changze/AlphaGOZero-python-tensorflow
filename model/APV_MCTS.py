"""
*** PROFILER RESULTS ***
suggest_move_prob (/Users/yuhang/Desktop/AlphaGOZero-python-tensorflow/model/APV_MCTS.py:160)
function called 1 times


         5840512 function calls (5832268 primitive calls) in 3.911 seconds

   Ordered by: cumulative time, internal time, call count
   List reduced from 208 to 40 due to restriction <40>

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.002    0.002    3.914    3.914 APV_MCTS.py:160(suggest_move_prob)
        1    0.013    0.013    3.896    3.896 {method 'run_until_complete' of 'uvloop.loop.Loop' objects}
     4440    0.009    0.000    3.857    0.001 APV_MCTS.py:270(tree_search)
9548/2904    0.046    0.000    3.830    0.001 APV_MCTS.py:181(start_tree_search)
     1305    0.009    0.000    1.823    0.001 APV_MCTS.py:130(expand)
     1305    0.625    0.000    1.807    0.001 APV_MCTS.py:133(<dictcomp>)
     3801    0.497    0.000    1.570    0.000 {built-in method builtins.max}
  1375962    0.364    0.000    1.072    0.000 APV_MCTS.py:258(<lambda>)
   472410    0.713    0.000    0.713    0.000 APV_MCTS.py:91(__init__)
  1375962    0.407    0.000    0.708    0.000 APV_MCTS.py:107(action_score)
   472410    0.393    0.000    0.471    0.000 index_tricks.py:516(__next__)
  1375962    0.301    0.000    0.301    0.000 APV_MCTS.py:103(Q)
     1600    0.003    0.000    0.146    0.000 APV_MCTS.py:123(compute_position)
     1600    0.013    0.000    0.142    0.000 go.py:354(play_move)
     1305    0.006    0.000    0.131    0.000 features.py:116(extract_features)
     1305    0.002    0.000    0.090    0.000 features.py:117(<listcomp>)
   472410    0.077    0.000    0.077    0.000 {built-in method builtins.next}
     1305    0.042    0.000    0.073    0.000 features.py:90(player_opponent_recent_eight_move)
3200/1600    0.011    0.000    0.065    0.000 copy.py:132(deepcopy)
     1600    0.007    0.000    0.055    0.000 go.py:269(__deepcopy__)
     6492    0.009    0.000    0.041    0.000 fromnumeric.py:55(_wrapfunc)
     2608    0.021    0.000    0.038    0.000 function_base.py:54(rot90)
    12652    0.035    0.000    0.035    0.000 {built-in method numpy.core.multiarray.array}
"""
from _asyncio import Future
import asyncio
from asyncio.queues import Queue
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from profilehooks import profile
import logging

import sys
import time
import numpy as np
from numpy.random import dirichlet,gamma
from collections import namedtuple
import logging
import daiquiri

daiquiri.setup(level=logging.DEBUG)
logger = daiquiri.getLogger(__name__)

import utils.go as go
from utils.features import extract_features,bulk_extract_features

# All terminology here (Q, U, N, p_UCT) uses the same notation as in the
# AlphaGo paper.
# Exploration constant
c_PUCT = 5

NOW_EXPANDING = set()
# queue size should be >= the number of semmphores
# in order to maxout the coroutines
# There is not rule of thumbs to choose optimal semmphores
# And keep in mind: the more coroutines, the less (?) quality (?)
# of the Monte Carlo Tree obtains. As my searching is less deep
# w.r.t a sequential MCTS. However, since MCTS is a randomnized
# algorithm that tries to approximate a value by averaging over run_many
# random processes, the quality of the search tree is hard to define.
# It's a trade off among time, accuracy, and the frequency of NN updates.
SEM = asyncio.Semaphore(64)
QUEUE = Queue(64)
LOOP = asyncio.get_event_loop()
RUNNING_SIMULATION_NUM = 0
QueueItem = namedtuple("QueueItem", "feature future")

class NetworkAPI(object):

    def __init__(self, net):
        self.net = net

    async def prediction_worker(self):
        """For better performance, queueing prediction requests and predict together in this worker.
        speed up about 45sec -> 15sec for example.
        """
        global QUEUE
        global RUNNING_SIMULATION_NUM
        q = QUEUE
        margin = 10  # avoid finishing before other searches starting.
        while RUNNING_SIMULATION_NUM> 0 or margin > 0:
            if q.empty():
                if margin > 0:
                    margin -= 1
                await asyncio.sleep(1e-3)
                continue
            item_list = [q.get_nowait() for _ in range(q.qsize())]  # type: list[QueueItem]
            #logger.debug(f"predicting {len(item_list)} items")
            bulk_features = np.asarray([item.feature for item in item_list])
            policy_ary, value_ary = self.run_many(bulk_features)
            for p, v, item in zip(policy_ary, value_ary, item_list):
                item.future.set_result((p, v))

    async def push_queue(self, features):
        global QUEUE
        global LOOP
        future = LOOP.create_future()
        item = QueueItem(features, future)
        await QUEUE.put(item)
        return future

    #@profile
    def run_many(self,bulk_features):
        return self.net.run_many(bulk_features)
        """simulate I/O & evaluate"""
        #sleep(np.random.random()*5e-2)
        #return np.random.random((len(bulk_features),362)), np.random.random((len(bulk_features),1))

class MCTSPlayerMixin(object):

    __slot__ = ["api","parent","move","prior","position","children","U",
                "N","W"]

    def __init__(self, network_api, parent, move, prior):
        self.api = network_api
        self.parent = parent # pointer to another MCTSNode
        self.move = move # the move that led to this node
        self.prior = prior
        self.position = None # lazily computed upon expansion
        self.children = {} # map of moves to resulting MCTSNode
        self.U,self.N,self.W = 0,0,0

    def __repr__(self):
        return f"<MCTSNode move=self.move prior=self.prior score=self.action_score is_expanded=self.is_expanded()>"

    @property
    def Q(self):
        return self.W/self.N if self.N !=0 else 0

    @property
    def action_score(self):
        return self.Q + self.U

    def virtual_loss_do(self):
        self.N += 3
        self.W -= 3

    def virtual_loss_undo(self):
        self.N -= 3
        self.W += 3

    def is_expanded(self):
        return self.position is not None

    #@profile
    def compute_position(self):
        """Evolve the game board, and return current position"""
        position = self.parent.position.play_move(self.move)
        self.position = position
        return position

    #@profile
    def expand(self, move_probabilities):
        """Expand leaf node"""
        #api,parent = self.api,self
        self.children = {move: MCTSPlayerMixin(self.api,self,move,prob)
            for move, prob in np.ndenumerate(np.reshape(move_probabilities[:-1],(go.N,go.N)))}
        # Pass should always be an option! Say, for example, seki.
        self.children[None] = MCTSPlayerMixin(self.api,self,None,move_probabilities[-1])

    def backup_value_single(self,value):
        """Backup value of a single tree node"""
        self.N += 1
        if self.parent is None:

            # No point in updating Q / U values for root, since they are
            # used to decide between children nodes.
            return

        # This incrementally calculates node.Q = average(Q of children),
        # given the newest Q value and the previous average of N-1 values.
        self.W, self.U = (
            self.W + value,
            c_PUCT * np.sqrt(self.parent.N) * self.prior / self.N,
        )
        #self.Q = self.W/self.N

    def move_prob(self):
        prob = np.asarray([child.N for child in self.children.values()]) / self.N
        prob /= np.sum(prob) # ensure 1.
        return prob

    @profile
    def suggest_move_prob(self, position, iters=1600):
        """Async tree search controller"""
        global LOOP

        start = time.time()

        if self.parent is None:
            move_probs,_ = self.api.run_many(bulk_extract_features([position]))
            self.position = position
            self.expand(move_probs[0])

        coroutine_list = []
        for _ in range(iters):
            coroutine_list.append(self.tree_search())
        coroutine_list.append(self.api.prediction_worker())
        LOOP.run_until_complete(asyncio.gather(*coroutine_list))

        logger.debug(f"Searched for {(time.time() - start):.5f} seconds")
        return self.move_prob()

    async def start_tree_search(self):
        global NOW_EXPANDING

        #TODO: add proper game over condition

        # add virtual loss
        self.virtual_loss_do()

        while self in NOW_EXPANDING:
            await asyncio.sleep(1e-4)

        if not self.is_expanded(): #  is leaf node

            # add leaf node to expanding list
            NOW_EXPANDING.add(self)

            # compute leaf node position
            pos = self.compute_position()

            if pos is None:
                #print("illegal move!", file=sys.stderr)
                # See go.Position.play_move for notes on detecting legality
                # In Go, illegal move means loss (or resign)
                # subtract virtual loss imposed at the beginnning
                self.virtual_loss_undo()
                self.backup_value_single(-1)
                NOW_EXPANDING.remove(self)
                return -1*-1

            """Show thinking history for fun"""
            #logger.debug(f"Investigating following position:\n{self.position}")

            # perform dihedral manipuation
            flip_axis,num_rot = np.random.randint(2),np.random.randint(4)
            dihedral_features = extract_features(pos,dihedral=[flip_axis,num_rot])

            # push extracted dihedral features of leaf node to the evaluation queue
            future = await self.api.push_queue(dihedral_features)  # type: Future
            await future
            move_probs, value = future.result()

            # perform reversed dihedral maniputation to move_prob
            move_probs = np.append(np.reshape(np.flip(np.rot90(np.reshape(\
            move_probs[:-1],(go.N,go.N)),4-num_rot),axis=flip_axis),(go.N**2,)),move_probs[-1])

            # expand by move probabilities
            self.expand(move_probs)

            # subtract virtual loss imposed at the beginnning
            self.virtual_loss_undo()

            # back up value just for current tree node
            self.backup_value_single(value[0])

            # remove leaf node from expanding list
            NOW_EXPANDING.remove(self)

            # must invert, because alternative layer has opposite objective
            return value[0]*-1

        else: # not a leaf node

            '''
            # perform dirichlet perturbed action score
            all_action_score = [child.Q + \
            child.U*(0.75+0.25*(noise)/(child.prior+1e-8)) for child,noise in\
            zip(self.children.values(),dirichlet([0.03]*362))]

            move2action_score = {move:action_score for move,action_score in \
            zip(self.children.keys(),all_action_score)}

            # select the move with maximum action score
            select_move = max(move2action_score, key=move2action_score.get)
            # start async tree search from child node
            # select_move = (np.random.randint(19), np.random.randint(19))
            value = await self.children[select_move].start_tree_search()
            '''
            child = max(self.children.values(), key=lambda node: node.action_score)
            value = await child.start_tree_search()

            # subtract virtual loss imposed at the beginning
            self.virtual_loss_undo()

            # back up value just for current node
            self.backup_value_single(value)

            # must invert
            return value*-1

    async def tree_search(self):
        """Independent tree search, stands for one simulation"""
        global RUNNING_SIMULATION_NUM
        global SEM

        RUNNING_SIMULATION_NUM += 1

        # reduce parallel search number
        with await SEM:

            value = await self.start_tree_search()

            #logger.debug(f"value: {value}")
            #logger.debug(f'Current running threads : {RUNNING_SIMULATION_NUM}')

            RUNNING_SIMULATION_NUM -= 1

            return value
