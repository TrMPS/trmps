import tensorflow as tf
import numpy as np
import sys
import math
import os

from tensorflow.examples.tutorials.mnist import input_data

def _preprocess_images(data, size, shrink = True):
    # Function to process images into format from paper, also currently just returns the images for zeroes and ones,
    # so that we can create a binary classifier first.
    # Returned as a list of [images, classifications]


    # written this way because originally, this was the only function and would read directly.
    mnist = data

    sess = tf.Session()
    data = []
    labels = []

    # Tensorflow operators / placeholders to resize the data from MNIST to format from paper
    # Resize images from 28*28 to 14*14
    image = tf.placeholder(tf.float32, shape=[784])
    if shrink:
        reshaped_image = tf.reshape(image, [-1, 28, 28, 1])
        pool = tf.nn.avg_pool(reshaped_image, ksize=[1, 2, 2, 1],
                              strides=[1, 2, 2, 1], padding='SAME')
        #pooled_image = tf.placeholder(tf.float32, shape=[1, 14, 14, 1])
        snaked_image = tf.reshape(pool, shape=[196])
    
        ones = tf.ones([196], dtype=tf.float32)
    else:
        snaked_image = image
        ones = tf.ones([784], dtype=tf.float32)
    phi = tf.stack([ones, snaked_image], axis=1)

    print("0 % done", end="")

    # Loop through all the elements in the dataset and resize
    with sess.as_default():
        sess.run(tf.global_variables_initializer())
        writer = tf.summary.FileWriter("output", sess.graph)
        writer.close()
        counter = 0

        for i in range(20):
            sys.stdout.flush()
            print("\r" + str(int((i / 20) * 100)) + " % done", end="")
            batch = mnist.next_batch(int(size / 20), shuffle=False)
            images = batch[0]
            for index, element in enumerate(images):
                #pooled = sess.run(pool,
                #                  feed_dict={reshaped_image: sess.run(reshaped_image,
                #                                                      feed_dict={image: element})})
                data.append(np.array(sess.run(phi, feed_dict={image: element})))
                labels.append(np.array(batch[1][index]))
                if index % 300 == 0:
                    # Spinner to show progress 
                    if counter == 0:
                        print("\r" + str(int((i / 20) * 100)) + " % done", end="|")
                        counter += 1
                    elif counter == 1:
                        print("\r" + str(int((i / 20) * 100)) + " % done", end="/")
                        counter += 1
                    elif counter == 2:
                        print("\r" + str(int((i / 20) * 100)) + " % done", end="-")
                        counter += 1
                    elif counter == 3:
                        print("\r" + str(int((i / 20) * 100)) + " % done", end="\\")
                        counter = 0
    sys.stdout.flush()
    print("\r" + str(100) + " % done")
    return (np.array(data), np.array(labels))

class MPSDatasource(object):
    def __init__(self, _expected_shape = None, shuffled = False):
        self._training_data = None
        self._test_data = None
        self._training_data_path = "training_data" + type(self).__name__ + ".npy"
        self._training_labels_path = "training_labels" + type(self).__name__ + ".npy"
        self._expected_shape = _expected_shape
        if os.path.isfile(self._training_data_path):
            self._training_data = (np.load(self._training_data_path), np.load(self._training_labels_path))
            print(self._training_data[0][0].shape)
            print(self._expected_shape)
            if self._training_data[0][0].shape != self._expected_shape:
                self._training_data = None
        self._test_data_path = "testing_data" + type(self).__name__ + ".npy"
        self._test_labels_path = "testing_labels" + type(self).__name__ + ".npy"
        if os.path.isfile(self._test_data_path):
            self._test_data = (np.load(self._test_data_path), np.load(self._test_labels_path))
            if self._test_data[0][0].shape != self._expected_shape:
                self._test_data = None
        self.current_index = 0
        if self._test_data == None:
            self._load_test_data()
        if self._training_data == None:
            self._load_training_data()
        if shuffled:
            data, labels = self._training_data
            permutation = np.random.permutation(len(data))
            self._training_data = data[permutation], labels[permutation]
        self._swapped_test_data = None
        self._swapped_training_data = None
        
    
    @property
    def test_data(self):
        if self._test_data is None:
            self._load_test_data()
        if self._swapped_test_data is None:
            self._swapped_test_data = np.swapaxes(self._test_data[0], 0, 1)
        return self._swapped_test_data, self._test_data[1]
    
    def _load_test_data(self):
        """
        Get test data (perhaps from remote server) and preprocess in shape [batch, expected shape of element]
        """
        return None
        
    @property
    def training_data(self):
        if self._training_data is None:
            self._load_training_data()
        if self._swapped_training_data is None:
            self._swapped_training_data = np.swapaxes(self._training_data[0], 0, 1)
        return self._swapped_training_data, self._training_data[1]
        
    def _load_training_data(self):
        """
        Get training data (perhaps from remote server) and preprocess in shape [batch, expected shape of element]
        """
        return None
        
    def next_training_data_batch(self, batch_size):
        if self._training_data == None:
            self._load_test_data()
        all_data, all_labels = self._training_data
        if batch_size > len(all_data):
            print("Probably shouldn't do this; your batch size is greater than the size of the dataset")
        data = None
        labels = None
        while batch_size > 0:
            if len(all_data) - self.current_index < batch_size:
                # print("A" + str(self.current_index))
                batch_size -= (len(all_data) - self.current_index)
                if self.current_index != len(all_data):
                    if data is None:
                        data = np.array(all_data[self.current_index:])
                        labels = np.array(all_labels[self.current_index:])
                    else:
                        data = np.concatenate((data, all_data[self.current_index:]), axis=0)
                        labels = np.concatenate((labels, all_labels[self.current_index:]), axis=0)
                self.current_index = 0
            else:
                # print("B" + str(self.current_index))
                if data is None:
                    data = all_data[self.current_index:self.current_index + batch_size]
                    labels = np.array(all_labels[self.current_index:self.current_index + batch_size])
                else:
                    data = np.concatenate((data, all_data[self.current_index:self.current_index + batch_size]), axis=0)
                    labels = np.concatenate((labels, all_labels[self.current_index:self.current_index + batch_size]),
                                            axis=0)
                self.current_index += batch_size
                batch_size = 0
        data = np.array(data)
        data = np.swapaxes(data, 0, 1)
        return (data, labels)
        
class MNISTDatasource(MPSDatasource):
    def __init__(self, shrink = True, permuted = False, shuffled = False):
        expected_size = (784, 2)
        if shrink:
            expected_size = (196,2)
        self.shrink = shrink
        super().__init__(expected_size, shuffled)
        if permuted:
            print("permuting")
            data, labels = self._training_data
            print(len(data[0]))
            permutation = np.random.permutation(len(data[0]))
            permuted_data = []
            for d in data:
                permuted_data.append(np.array(d[permutation]))
            self._training_data = np.array(permuted_data), labels
            test_data, test_labels = self._test_data
            permuted_test_data = []
            for d in test_data:
                permuted_test_data.append(np.array(d[permutation]))
            self._test_data = np.array(permuted_test_data), test_labels
            
    def _load_test_data(self):
        self._test_data = _preprocess_images(input_data.read_data_sets('MNIST_data', one_hot=True).test, size=10000, shrink=self.shrink)
        np.save(self._test_data_path, self._test_data[0])
        np.save(self._test_labels_path, self._test_data[1])
    
    def _load_training_data(self):
        self._training_data = _preprocess_images(input_data.read_data_sets('MNIST_data', one_hot=True).train,
                                                     size=60000, shrink=self.shrink)
        np.save(self._training_data_path, self._training_data[0])
        np.save(self._training_labels_path, self._training_data[1])


if __name__ == "__main__":
    # If main, processes the images and also prints the number of images
    data_source = MNISTDatasource(shrink = False)
    data, labels = data_source.next_training_data_batch(1000)
    print(data.shape)
    print(len(labels))
    data, labels = data_source.next_training_data_batch(1000)
    print(data.shape)
    print(len(labels))
    data, labels = data_source.next_training_data_batch(1000)
    print(len(labels))
    data, labels = data_source.next_training_data_batch(500)
