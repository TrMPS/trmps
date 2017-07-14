import tensorflow as tf
import time
import preprocessing
from mps import MPS
import pickle
import utils
from tensorflow.python.client import timeline


def list_from(tensorArray, length):
    arr = tensorArray
    result_list = []
    with tf.name_scope("createlist"):
        for i in range(length):
            result_list.append(arr.read(i))
    return result_list


class MPSOptimizer(object):

    def __init__(self, MPSNetwork, max_size, grad_func, cutoff=10 ** (-5)):
        self.MPS = MPSNetwork
        self.rate_of_change = tf.placeholder(tf.float32, shape=[])
        self.max_size = max_size
        self.grad_func = grad_func
        self.cutoff = cutoff
        self._feature = tf.placeholder(tf.float32, shape=[input_size, None, self.MPS.d_feature])
        self._label = tf.placeholder(tf.float32, shape=[None, self.MPS.d_output])
        self.MPS._setup_nodes(self._feature)
        self._setup_optimization()
        _ = self.train_step()

    def train(self, data_source, batch_size, n_step, rate_of_change=1000, logging_enabled=None, initial_weights=None):
        _logging_enabled = logging_enabled
        if logging_enabled is None:
            _logging_enabled = False

        run_options = []
        run_metadata = []
        if _logging_enabled:
            run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            run_metadata = tf.RunMetadata()

        #increment = (1/10) ** (1/(n_step))
        increment = 1.

        self.feed_dict = None
        self.test = None
        f = self.MPS.predict(self._feature)
        cost = self.MPS.cost(f, self._label)
        accuracy = self.MPS.accuracy(f, self._label)
        test_result = list_from(self.updated_nodes, length=self.MPS.input_size)
        self.test = initial_weights

        with tf.Session() as sess:
            sess.run(tf.global_variables_initializer())
            if _logging_enabled:
                writer = tf.summary.FileWriter("output", sess.graph)
            for i in range(n_step):
                start = time.time()
                (batch_feature, batch_label) = data_source.next_training_data_batch(batch_size)
                self.feed_dict = self.MPS.create_feed_dict(self.test)
                self.feed_dict[self._feature] = batch_feature
                self.feed_dict[self._label] = batch_label
                self.feed_dict[self.rate_of_change] = rate_of_change 
                train_cost, prediction, self.test, train_accuracy = sess.run([cost, f, test_result, accuracy],
                                                                             feed_dict=self.feed_dict,
                                                                             options=run_options,
                                                                             run_metadata=run_metadata)

                rate_of_change = rate_of_change * increment

                self.feed_dict = {self._feature: batch_feature, self._label: batch_label}
                for index, element in enumerate(self.test):
                    self.feed_dict[self.MPS.nodes_list[index]] = element
                if _logging_enabled:
                    #writer.add_run_metadata(run_metadata, 'step' + str(i))
                    tl = timeline.Timeline(run_metadata.step_stats)
                    ctf = tl.generate_chrome_trace_format()
                    with open("timeline.json", "w") as f:
                        f.write(ctf)
                    run_metadata = tf.RunMetadata()
                with open('weights', 'wb') as fp:
                    pickle.dump(self.test, fp)
                end = time.time()
                print('step {}, training cost {}, accuracy {}. Took {} s'.format(i, train_cost, train_accuracy, end - start))
                #print("prediction:" + str(prediction[0]))
            if _logging_enabled:
                writer.close()

    def _setup_optimization(self):
        '''
        C1s: size = input_size - 2 (as the last one is kept redundant) 
        C2s: size = input_size - 2 (first one redundant)

        writes C1 from 0 to special_loc-1 
        writes C2 from special_loc to size-3 (corresponds to special_loc+2 to size-1 the nodes)
        '''
        feature = self._feature
        nodes = self.MPS.nodes
        special_loc = self.MPS._special_node_loc
        with tf.name_scope("setup_optimization"):
            n1 = nodes.read(0)
            n1.set_shape([None, None])
            C1 = tf.einsum('ni,tn->ti', n1, feature[0])
            C1s = tf.TensorArray(tf.float32, size=self.MPS.input_size - 2, infer_shape=False, clear_after_read=False)
            C1s = C1s.write(0, C1)
            cond = lambda c, *args: tf.less(c, special_loc)
            _, _, self.C1s = tf.while_loop(cond=cond, body=self._find_C1, loop_vars=[1, C1, C1s],
                                           shape_invariants=[tf.TensorShape([]), tf.TensorShape([None, None]),
                                                             tf.TensorShape(None)],
                                           parallel_iterations=10,
                                           name="initialFindC1")

            nlast = nodes.read(nodes.size() - 1)
            nlast.set_shape([None, None])
            C2s = tf.TensorArray(tf.float32, size=self.MPS.input_size - 2, infer_shape=False, clear_after_read=False)
            C2 = tf.einsum('mi,tm->ti', nlast, feature[-1])
            C2s = C2s.write(self.MPS.input_size - 3, C2)
            cond = lambda counter, *args: tf.less(counter, self.MPS.input_size - special_loc - 1)
            _, _, self.C2s = tf.while_loop(cond=cond, body=self._find_C2, loop_vars=[1, C2, C2s],
                                           shape_invariants=[tf.TensorShape([]), tf.TensorShape([None, None]),
                                                             tf.TensorShape(None)], parallel_iterations=10,
                                           name="initialFindC2")

    def _find_C1(self, counter, C1, C1s):
        '''
        finds new C1, and write into C1s[counter]
        '''
        node = self.MPS.nodes.read(counter)
        node.set_shape([self.MPS.d_feature, None, None])
        input_leg = self._feature[counter]
        # contracted_node = tf.einsum('mij,tm->tij', node, input_leg)
        contracted_node = tf.tensordot(input_leg, node, [[1], [0]])
        C1 = tf.einsum('ti,tij->tj', C1, contracted_node)
        C1s = C1s.write(counter, C1)
        counter = counter + 1
        return [counter, C1, C1s]

    def _find_C2(self, counter, prev_C2, C2s):
        '''
        finds new C2, and write into C2s[counter]
        '''
        loc2 = self.MPS.input_size - 1 - counter
        node2 = self.MPS.nodes.read(loc2)
        node2.set_shape([self.MPS.d_feature, None, None])
        # contracted_node2 = tf.einsum('mij,tm->tij', node2, self._feature[loc2])  # CHECK einsum
        contracted_node2 = tf.tensordot(self._feature[loc2], node2, [[1], [0]])
        updated_counter = counter + 1
        new_C2 = tf.einsum('tij,tj->ti', contracted_node2, prev_C2)
        C2s = C2s.write(self.MPS.input_size - 3 - counter, new_C2)
        return [updated_counter, new_C2, C2s]

    def train_step(self):
        #Parallel version of train_step
        self.batch_size = tf.shape(self._feature)[1]

        with tf.name_scope("parllel_train_step"):
            # Create updated_nodes and fill in the first half from current one
            original_special_node_loc = self.MPS._special_node_loc
            self.updated_nodes_r = self._duplicate_nodes(self.MPS.nodes, original_special_node_loc - 2, original_special_node_loc)
            self.updated_nodes_l = self._duplicate_nodes(self.MPS.nodes, 0, 0)
            nodes_l, nodes_r, inv_s = self._split_nodes(self.MPS.nodes, original_special_node_loc)

            # Right hand side
            # Front sweep
            self.updated_nodes_r, self.C1s_r, self.C2s_r = self._sweep_right(self.MPS._special_node_loc, self.MPS.input_size - 2, self.updated_nodes_r,
                                                                        self.C1s, self.C2s, nodes_r)
            # Back sweep
            C2_r = self.C2s_r.read(self.MPS.input_size - 3)
            self.C2s_r = tf.TensorArray(tf.float32, size=self.MPS.input_size - 2, infer_shape=False, clear_after_read=False)
            self.C2s_r = self.C2s_r.write(self.MPS.input_size - 3, C2_r)
            self.updated_nodes_r2 = self._duplicate_nodes(self.MPS.nodes, 0, 0)
            self.updated_nodes_r2, self.C1s_r, self.C2s_r = self._sweep_left(self.MPS.nodes.size() - 2, original_special_node_loc, self.updated_nodes_r2,
                                                                    self.C1s_r, self.C2s_r, self.updated_nodes_r)

            # Left hand side
            # Back sweep
            self.updated_nodes_l, self.C1s_l, self.C2s_l = self._sweep_left(self.MPS._special_node_loc, 1, self.updated_nodes_l,
                                                                        self.C1s, self.C2s, nodes_l)
            # Front sweep
            C1_l = self.C1s.read(0)
            self.C1s_l = tf.TensorArray(tf.float32, size=self.MPS.input_size - 2, dynamic_size=True, infer_shape=False,
                                      clear_after_read=False)
            self.C1s_l = self.C1s_l.write(0, C1_l)
            self.updated_nodes_l2 = self._duplicate_nodes(self.MPS.nodes, 0, 0)
            self.updated_nodes_l2, self.C1s_l, self.C2s_l  = self._sweep_right(1, original_special_node_loc, self.updated_nodes_l2,
                                                            self.C1s_l, self.C2s_l, self.updated_nodes_l)

            center1 = self.updated_nodes_l2.read(original_special_node_loc)
            center1.set_shape([None, None, None, None])
            center1 = tf.Print(center1, [tf.shape(center1), tf.shape(inv_s)], message = "center1 shape", summarize = 1000)
            center1 = tf.einsum('lmj,lmij->lmij', inv_s, center1)
            center2 = self.updated_nodes_r2.read(original_special_node_loc)
            center2.set_shape([None, None, None, None])
            center = tf.matmul(center1, center2)
            #center1 = tf.Print(center1, [tf.shape(center1), tf.shape(center2)], summarize = 100, message = "shapes")
            self.updated_nodes = self._merge_nodes(self.updated_nodes_l2, self.updated_nodes_r2, original_special_node_loc)
            center = tf.Print(center, [tf.shape(center), tf.shape(self.updated_nodes.read(original_special_node_loc - 1)),
                tf.shape(self.updated_nodes.read(original_special_node_loc + 1))], summarize = 100, message = "center shape")
            self.updated_nodes = self.updated_nodes.write(original_special_node_loc, center)
            self.MPS.nodes = self.updated_nodes
            self.MPS._special_node_loc = original_special_node_loc
            
            # accuracy
            f = self.MPS.predict(self._feature)
            accuracy = self.MPS.accuracy(f, self._label)

        return accuracy

    def train_step_serial(self):
        self.batch_size = tf.shape(self._feature)[1]

        with tf.name_scope("train_step"):
            # Create updated_nodes and fill in the first half from current one
            self.updated_nodes = self._duplicate_nodes(self.MPS.nodes, 0, self.MPS._special_node_loc)
            original_special_node_loc = self.MPS._special_node_loc

            # First half-sweep
            self.updated_nodes, self.C1s, self.C2s = self._sweep_right(self.MPS._special_node_loc, self.MPS.input_size - 2, self.updated_nodes,
                                                                        self.C1s, self.C2s, self.MPS.nodes)
            self.MPS.nodes = self.updated_nodes
            self.MPS._special_node_loc = self.MPS.nodes.size() - 2

            # First back-sweep
            C2 = self.C2s.read(self.MPS.input_size - 3)
            self.C2s = tf.TensorArray(tf.float32, size=self.MPS.input_size - 2, infer_shape=False, clear_after_read=False)
            self.C2s = self.C2s.write(self.MPS.input_size - 3, C2)
            self.updated_nodes = self._duplicate_nodes(self.MPS.nodes, 0, 0)
            self.updated_nodes, self.C1s, self.C2s = self._sweep_left(self.MPS.nodes.size() - 2, 1, self.updated_nodes,
                                                                    self.C1s, self.C2s, self.MPS.nodes)
            self.MPS.nodes = self.updated_nodes
            self.MPS._special_node_loc = 1
            
            # Second half-sweep
            self.updated_nodes = self._duplicate_nodes(self.MPS.nodes, original_special_node_loc + 1,
                                                       self.MPS.nodes.size() + 10)
            C1 = self.C1s.read(0)
            self.C1s = tf.TensorArray(tf.float32, size=self.MPS.input_size - 2, dynamic_size=True, infer_shape=False,
                                      clear_after_read=False)
            self.C1s = self.C1s.write(0, C1)
            self.updated_nodes, self.C1s, self.C2s  = self._sweep_right(1, original_special_node_loc, self.updated_nodes,
                                                            self.C1s, self.C2s, self.MPS.nodes)
            self.MPS.nodes = self.updated_nodes
            self.MPS._special_node_loc = original_special_node_loc
            
            # accuracy
            f = self.MPS.predict(self._feature)
            accuracy = self.MPS.accuracy(f, self._label)

        return accuracy

    def _sweep_left(self, from_index, to_index, updated_nodes, C1s, C2s, nodes):
        # read second from end node
        n1 = nodes.read(from_index)
        n1.set_shape([self.MPS.d_output, self.MPS.d_feature, None, None])

        cond = lambda counter, b, c, d, e, f: tf.greater(counter, to_index)
        wrapped = [from_index, C1s, C2s, updated_nodes, nodes, n1]
        shape_invariants = [tf.TensorShape([]), tf.TensorShape(None), tf.TensorShape(None),
                            tf.TensorShape(None), tf.TensorShape(None), tf.TensorShape([None, None, None, None])]

        counter, C1s, C2s, updated_nodes, _, n1 = tf.while_loop(cond=cond, body=self._update_left,
                                                                               loop_vars=wrapped,
                                                                               shape_invariants=shape_invariants,
                                                                               parallel_iterations=10,
                                                                               name="backwardsSweep")
        with tf.control_dependencies([counter]):
            updated_nodes = updated_nodes.write(to_index, n1)
        return updated_nodes, C1s, C2s

    def _sweep_right(self, from_index, to_index, updated_nodes, C1s, C2s, nodes):
        n1 = nodes.read(from_index)
        n1.set_shape([self.MPS.d_output, self.MPS.d_feature, None, None])
        cond = lambda counter, b, c, d, e, f: tf.less(counter, to_index)
        wrapped = [from_index, C1s, C2s, updated_nodes, nodes, n1]
        shape_invariants = [tf.TensorShape([]), tf.TensorShape(None), tf.TensorShape(None),
                            tf.TensorShape(None), tf.TensorShape(None), tf.TensorShape([None, None, None, None])]

        _, C1s, C2s, updated_nodes, _, n1 = tf.while_loop(cond=cond, body=self._update_right,
                                                                         loop_vars=wrapped,
                                                                         shape_invariants=shape_invariants,
                                                                         parallel_iterations=10, name="rightSweep")
        updated_nodes = updated_nodes.write(to_index, n1)
        return updated_nodes, C1s, C2s

    def _update_left(self, counter, C1s, C2s, updated_nodes, nodes, previous_node):

        with tf.name_scope("update_left"):
            # Read in the nodes 
            n1 = previous_node
            n2 = nodes.read(counter - 1)
            n2.set_shape([self.MPS.d_feature, None, None])

            # Calculate the C matrix 
            C2 = C2s.read(counter - 1)
            C1 = C1s.read(counter - 2)
            C1.set_shape([None, None])
            C2.set_shape([None, None])
            input1 = self._feature[counter-1]
            input2 = self._feature[counter]

            # Calculate the bond 
            bond = tf.einsum('nkj,lmji->lmnik', n2, n1)

            C = self._calculate_C(C2, C1, input2, input1)

            # update the bond 
            updated_bond = self._update_bond(bond, C)

            # Decompose the bond 
            aj, aj1 = self._bond_decomposition(updated_bond, self.max_size)
            aj = tf.transpose(aj, perm=[0, 2, 1])
            aj1 = tf.transpose(aj1, perm=[1, 2, 3, 0])

            # Transpose the values and add to the new variables 
            updated_nodes = updated_nodes.write(counter, aj)
            with tf.name_scope("tensordotcontracted_aj"):
                #contracted_aj = tf.einsum('mij,tm->tij', aj, self._feature[counter])
                contracted_aj = tf.tensordot(self._feature[counter], aj, [[1], [0]])
            with tf.name_scope("einsumC2"):
                C2 = tf.einsum('tij,tj->ti', contracted_aj, C2)
            C2s = C2s.write(counter - 2, C2)
            updated_counter = counter - 1

        return [updated_counter, C1s, C2s, updated_nodes, nodes, aj1]


    def _update_right(self, counter, C1s, C2s, updated_nodes, nodes, previous_node):
        with tf.name_scope("update_right"):
            # Read in the nodes 
            n1 = previous_node
            n2 = nodes.read(counter + 1)
            n2.set_shape([self.MPS.d_feature, None, None])
    
            # Calculate the C matrix 
            C2 = C2s.read(counter)
            C1 = C1s.read(counter - 1)
            C1.set_shape([None, None])
            C2.set_shape([None, None])
            input1 = self._feature[counter]
            input2 = self._feature[counter+1]

            # Calculate the bond 
            bond = tf.einsum('lmij,njk->lmnik', n1, n2)
            # bond = tf.transpose(tf.tensordot(n1, n2, [[3],[1]]), [0, 1, 3, 2, 4])
            # einsum is actually faster in this case

            C = self._calculate_C(C1, C2, input1, input2)

            # Update the bond 
            updated_bond = self._update_bond(bond, C)
    
            # Decompose the bond 
            aj, aj1 = self._bond_decomposition(updated_bond, self.max_size)
            aj1 = tf.transpose(aj1, perm=[1, 2, 0, 3])
    
            # Transpose the values and add to the new variables 
            updated_nodes = updated_nodes.write(counter, aj)
    
            with tf.name_scope("tensordotcontracted_aj"):
                #contracted_aj = tf.einsum('mij,tm->tij', aj, self._feature[counter])
                contracted_aj = tf.tensordot(self._feature[counter], aj, [[1], [0]])
            with tf.name_scope("einsumC1"):
                C1 = tf.einsum('tij,ti->tj', contracted_aj, C1)
            C1s = C1s.write(counter, C1)
            updated_counter = counter + 1
    
        return [updated_counter, C1s, C2s, updated_nodes, nodes, aj1]

    def _calculate_C(self, C1, C2, input1, input2):
        # C = tf.einsum('ti,tk,tm,tn->tmnik', C1, C2, input1, input2)
        d1 = tf.shape(C1)[1]
        d2 = tf.shape(C2)[1]

        with tf.name_scope("calculateC"):
            C1 = tf.reshape(C1, [self.batch_size, 1, 1, d1, 1])
            C2 = tf.reshape(C2, [self.batch_size, 1, 1, 1, d2])
            input1 = tf.reshape(input1, [self.batch_size, self.MPS.d_feature, 1, 1, 1])
            input2 = tf.reshape(input2, [self.batch_size, 1, self.MPS.d_feature, 1, 1])
            intermediate_1 = C1 * C2
            intermediate_2 = input1 * input2
            C = intermediate_1 * intermediate_2

        return C

    def _get_f_and_cost(self, bond, C):
        with tf.name_scope("tensordotf"):
            #f = tf.einsum('lmnik,tmnik->tl', bond, C)
            f = tf.tensordot(C, bond, [[1,2,3,4],[1,2,3,4]])
        with tf.name_scope("reduce_sumcost"):
            cost = 0.5 * tf.reduce_sum(tf.square(f-self._label))

        return f, cost

    def _update_bond(self, bond, C):
        # obtain the original cost
        f, cost = self._get_f_and_cost(bond, C)

        # perform gradient descent on the bond 
        with tf.name_scope("tensordotgradient"):
            #gradient = tf.einsum('tl,tmnik->lmnik', self._label-f, C)
            gradient = tf.tensordot(self._label-f, C, [[0],[0]])
        label_bond = self.rate_of_change * gradient
        label_bond = tf.clip_by_value(label_bond, -(self.cutoff), self.cutoff)
        updated_bond = tf.add(bond, label_bond)
        
        # calculate the cost with the updated bond
        f1, cost1 = self._get_f_and_cost(updated_bond, C)
        #cost1 = tf.Print(cost1, [cost, cost1], message='cost and updated cost')
        cond_change_bond = tf.less(cost1, cost)
        updated_bond = tf.cond(cond_change_bond, true_fn=(lambda: updated_bond), false_fn=(lambda: bond))

        return updated_bond

    def _make_new_nodes(self, nodes):
        size = nodes.size()
        new_nodes = tf.TensorArray(tf.float32, size=size, infer_shape=False, clear_after_read=False)
        new_nodes = new_nodes.write(0, nodes.read(size - 1))
        new_nodes = new_nodes.write(size - 1, nodes.read(0))
        return new_nodes

    def _duplicate_nodes(self, nodes, from_index, to_index):
        '''
        duplicate the nodes in the range (from_index, to_index)
        '''
        with tf.name_scope("duplicatingNodes"):
            size = nodes.size()
            new_nodes = tf.TensorArray(tf.float32, size=size, infer_shape=False, clear_after_read=False)

            from_index = tf.cond(tf.equal(from_index, 0), lambda: 1, lambda: from_index)
            to_index = tf.cond(tf.greater(to_index, size - 1), lambda: size - 1, lambda: to_index)
            cond = lambda index, a, b: tf.less(index, to_index)

            _, _, new_nodes = tf.while_loop(cond=cond, body=self._transfer_to_array,
                                            loop_vars=[from_index, nodes, new_nodes], name="duplicate_loop")
            new_nodes = new_nodes.write(0, nodes.read(0))
            new_nodes = new_nodes.write(size - 1, nodes.read(size - 1))
        return new_nodes

    def _transfer_to_array(self, index, old_nodes, new_nodes):
        old_node = old_nodes.read(index)
        new_nodes = new_nodes.write(index, old_node)
        index += 1
        return (index, old_nodes, new_nodes)

    def _merge_nodes(self, nodes1, nodes2, special_loc):
        size = nodes1.size()
        new_nodes = tf.TensorArray(tf.float32, size=size, infer_shape=False, clear_after_read=False)
        cond = lambda index, a, b: tf.less(index, special_loc)
        cond2 = lambda index, a, b: tf.less(index, size)
        _, _, new_nodes = tf.while_loop(cond=cond, body=self._transfer_to_array,
                                            loop_vars=[0, nodes1, new_nodes], name="merge_loop1")
        _, _, new_nodes = tf.while_loop(cond=cond2, body=self._transfer_to_array,
                                            loop_vars=[special_loc+1, nodes2, new_nodes], name="merge_loop2")
        return new_nodes

    def _split_nodes(self, nodes, special_loc):
        n1 = nodes.read(special_loc)
        n1.set_shape([None, None, None, None])
        n1 = tf.Print(n1, [tf.shape(n1)], message = "n1 shape")
        l, r, inv_s = self._node_decomposition(n1, self.max_size)
        new_nodes_l = self._merge_nodes(nodes, nodes, special_loc)
        new_nodes_r = self._merge_nodes(nodes, nodes, special_loc)
        new_nodes_l = new_nodes_l.write(special_loc, l)
        new_nodes_r = new_nodes_r.write(special_loc, r)
        return new_nodes_l, new_nodes_r, inv_s


    def _bond_decomposition(self, bond, max_size, min_size=None, threshold=None):
        """
        Decomposes bond, so that the next step can be done.
        :param bond:
        :param m:
        :return:
        """
        if threshold is None:
            _threshold = 10 ** (-8)
        else:
            _threshold = threshold
        if min_size is None:
            min_size = 3
        else:
            min_size = min_size
        with tf.name_scope("bond_decomposition"):
            bond_reshaped = tf.transpose(bond, perm=[1, 3, 0, 2, 4])
            #bond_reshaped = tf.Print(bond_reshaped, [tf.shape(bond_reshaped), tf.shape(bond)], summarize = 1000, message = "bond reshaped, bond")

            dims = tf.shape(bond_reshaped)
            l_dim = dims[0] * dims[1]
            r_dim = dims[2] * dims[3] * dims[4]
            bond_flattened = tf.reshape(bond_reshaped, [l_dim, r_dim])
            s, u, v = tf.svd(bond_flattened)
            filtered_u = utils.check_nan(u, 'u', replace_nan=True)
            filtered_v = utils.check_nan(v, 'v', replace_nan=True)

            filtered_s = tf.boolean_mask(s, tf.greater(s, _threshold))
            s_size = tf.size(filtered_s)

            case1 = lambda: min_size
            case2 = lambda: max_size
            case3 = lambda: s_size
            m = tf.case({tf.less(s_size, min_size): case1, tf.greater(s_size, max_size): case2}, default=case3,
                        exclusive=True)

            # make s into a matrix
            s_mat = tf.diag(s[0:m])

            # make u, v into suitable matrices
            u_cropped = filtered_u[:, 0:m]
            v_cropped = tf.transpose(filtered_v[:, 0:m])

            # make a_ 
            a_prime_j = tf.reshape(u_cropped, [dims[0], dims[1], m])
            sv = tf.matmul(s_mat, v_cropped)
            a_prime_j1 = tf.reshape(sv, [m, dims[2], dims[3], dims[4]])
            #a_prime_j1 = tf.transpose(a_prime_j1_mixed, perm=[1, 2, 0, 3])
            # will do this in the update_right/update_left functions from now on as else transpose twice for udpate_left

        return (a_prime_j, a_prime_j1)

    def _node_decomposition(self, node, max_size, min_size=None, threshold=None):
        if threshold is None:
            _threshold = 10 ** (-8)
        else:
            _threshold = threshold
        if min_size is None:
            min_size = 3
        else:
            min_size = min_size
        with tf.name_scope("node_decomposition"):
            dims = tf.shape(node)
            l_dim = dims[2]
            r_dim = dims[3]
            node_flattened = tf.reshape(node, [dims[0] * dims[1], l_dim, r_dim])
            s, u, v = tf.svd(node_flattened)
            filtered_u = utils.check_nan(u, 'u', replace_nan=True)
            filtered_v = utils.check_nan(v, 'v', replace_nan=True)

            boolean = tf.greater(s, _threshold)
            boolean = tf.reduce_prod(tf.cast(boolean, tf.int32), 0)
            s_size = tf.reduce_sum(boolean)
            s_size = tf.Print(s_size, [s_size, tf.shape(s)], summarize = 1000, message = "satributes")

            case1 = lambda: min_size
            case2 = lambda: max_size
            case3 = lambda: s_size
            m = tf.case({tf.less(s_size, min_size): case1, tf.greater(s_size, max_size): case2}, default=case3,
                        exclusive=True)

            # make s into a matrix
            sliced_s = s[:,0:m]
            inv_s = tf.divide(1, sliced_s)

            # make u, v into suitable matrices
            u_cropped = filtered_u[:,:, 0:m]
            v_cropped = tf.transpose(filtered_v[:,:, 0:m], [0, 2, 1])

            # make a_ 
            sliced_s = tf.Print(sliced_s, [tf.shape(sliced_s), tf.shape(u_cropped), tf.shape(v_cropped)], summarize = 1000, message = "shapes of concern")
            with tf.control_dependencies([sliced_s]):
               su = tf.einsum('lk,ljk->ljk', sliced_s, u_cropped)
               a_prime_j = tf.reshape(su, [dims[0], dims[1], l_dim, m])
               sv = tf.einsum('lm,lmk->lmk', sliced_s, v_cropped)
               sv = tf.Print(sv, [tf.shape(a_prime_j), tf.shape(su)], summarize = 1000, message = "svshape")
               a_prime_j1 = tf.reshape(sv, [dims[0], dims[1], m, r_dim])
               inv_s = tf.reshape(inv_s, [dims[0], dims[1], m])
        #return node, sliced_s, inv_s
        return (a_prime_j, a_prime_j1, inv_s)



if __name__ == '__main__':
    # Model parameters
    input_size = 196
    d_feature = 2
    d_output = 10
    batch_size = 1000

    bond_dim = 3
    max_size = 8

    rate_of_change = 1000
    logging_enabled = True

    cutoff = 10
    n_step = 10

    data_source = preprocessing.MNISTData()

    # Initialise the model

    #with open('weights', 'rb') as fp:
    #    weights = pickle.load(fp)
    #    if len(weights) != input_size:
    #        weights = None

    weights = None
    network = MPS(bond_dim, d_feature, d_output, input_size)
    optimizer = MPSOptimizer(network, max_size, None, cutoff=cutoff)
    optimizer.train(data_source, batch_size, n_step, 
                    rate_of_change=rate_of_change, 
                    logging_enabled=logging_enabled, 
                    initial_weights=weights)





