from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys

import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf

from . import config
from . import metrics as metrics_module
from .callbacks import CallbackList
from .utils import timing


class Model(object):
    """Model
    """

    scipy_opts = ["BFGS", "L-BFGS-B", "Nelder-Mead", "Powell", "CG", "Newton-CG"]

    def __init__(self, data, net):
        self.data = data
        self.net = net

        self.optimizer = None
        self.batch_size, self.ntest = None, None

        self.losses, self.totalloss = None, None
        self.train_op = None
        self.metrics = None

        self.sess = None
        self.train_state = TrainState()
        self.losshistory = LossHistory()

    @timing
    def compile(
        self,
        optimizer,
        lr,
        batch_size,
        ntest,
        loss="MSE",
        metrics=None,
        decay=None,
        loss_weights=None,
    ):
        print("Compiling model...")

        self.optimizer = optimizer
        self.batch_size, self.ntest = batch_size, ntest

        self.losses = self.data.losses(self.net.targets, self.net.outputs, loss, self)
        if self.net.regularizer is not None:
            self.losses.append(tf.losses.get_regularization_loss())
        self.losses = tf.convert_to_tensor(self.losses)
        if loss_weights is not None:
            self.losses *= loss_weights
            self.losshistory.update_loss_weights(loss_weights)
        self.totalloss = tf.reduce_sum(self.losses)

        lr, global_step = self.get_learningrate(lr, decay)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            if self.optimizer in Model.scipy_opts:
                self.train_op = tf.contrib.opt.ScipyOptimizerInterface(
                    self.totalloss, method=self.optimizer, options={"disp": True}
                )
            else:
                self.train_op = self.get_optimizer(self.optimizer, lr).minimize(
                    self.totalloss, global_step=global_step
                )

        self.metrics = [metrics_module.get(m) for m in metrics]

    @timing
    def train(
        self,
        epochs,
        validation_every=1000,
        uncertainty=False,
        errstop=None,
        callbacks=None,
        print_model=False,
    ):
        print("Training model...")

        self.open_tfsession()
        self.sess.run(tf.global_variables_initializer())

        if print_model:
            self.print_model()
        if self.optimizer in Model.scipy_opts:
            self.train_scipy(uncertainty)
        else:
            self.train_sgd(epochs, validation_every, uncertainty, errstop, callbacks)
        if print_model:
            self.print_model()

        self.close_tfsession()
        return self.losshistory, self.train_state

    def open_tfsession(self):
        tfconfig = tf.ConfigProto()
        tfconfig.gpu_options.allow_growth = True
        self.sess = tf.Session(config=tfconfig)
        self.train_state.update_tfsession(self.sess)

    def close_tfsession(self):
        self.sess.close()

    def train_sgd(self, epochs, validation_every, uncertainty, errstop, callbacks):
        callbacks = CallbackList(callbacks=callbacks)

        self.train_state.update_data_test(*self.data.test(self.ntest))

        callbacks.on_train_begin(self.train_state)

        for i in range(epochs):
            callbacks.on_epoch_begin(self.train_state)
            callbacks.on_batch_begin(self.train_state)

            self.train_state.update_data_train(
                *self.data.train_next_batch(self.batch_size)
            )
            self.sess.run(
                self.train_op,
                feed_dict=self.get_feed_dict(
                    True, True, 0, self.train_state.X_train, self.train_state.y_train
                ),
            )

            self.train_state.epoch += 1
            self.train_state.step += 1

            if i % validation_every == 0 or i + 1 == epochs:
                self.test(uncertainty)

                self.losshistory.add(
                    i,
                    self.train_state.loss_train,
                    self.train_state.loss_test,
                    self.train_state.metrics_test,
                )
                print(
                    "Epoch: %d, loss: %s, val_loss: %s, val_metric: %s"
                    % (
                        i,
                        self.train_state.loss_train,
                        self.train_state.loss_test,
                        self.train_state.metrics_test,
                    )
                )
                sys.stdout.flush()

                # if errstop is not None and err_norm < errstop:
                #     break

            callbacks.on_batch_end(self.train_state)
            callbacks.on_epoch_end(self.train_state)

        callbacks.on_train_end(self.train_state)

    def train_scipy(self, uncertainty):
        self.train_state.update_data_train(*self.data.train_next_batch(self.batch_size))
        self.train_op.minimize(
            self.sess,
            feed_dict=self.get_feed_dict(
                True, True, 0, self.train_state.X_train, self.train_state.y_train
            ),
        )

        self.train_state.update_data_test(*self.data.test(self.ntest))
        self.test(uncertainty)
        self.losshistory.add(
            1,
            self.train_state.loss_train,
            self.train_state.loss_test,
            self.train_state.metrics_test,
        )
        print(
            "loss: %s, val_loss: %s, val_metric: %s"
            % (
                self.train_state.loss_train,
                self.train_state.loss_test,
                self.train_state.metrics_test,
            )
        )
        sys.stdout.flush()

    def test(self, uncertainty):
        self.train_state.loss_train, self.train_state.y_pred_train = self.sess.run(
            [self.losses, self.net.outputs],
            feed_dict=self.get_feed_dict(
                False, False, 0, self.train_state.X_train, self.train_state.y_train
            ),
        )

        if uncertainty:
            # TODO: support multi outputs
            losses, y_preds = [], []
            for _ in range(1000):
                loss_one, y_pred_test_one = self.sess.run(
                    [self.losses, self.net.outputs],
                    feed_dict=self.get_feed_dict(
                        False, True, 1, self.train_state.X_test, self.train_state.y_test
                    ),
                )
                losses.append(loss_one)
                y_preds.append(y_pred_test_one)
            self.train_state.loss_test = np.mean(losses, axis=0)
            self.train_state.y_pred_test = np.mean(y_preds, axis=0)
            self.train_state.y_std_test = np.std(y_preds, axis=0)
        else:
            self.train_state.loss_test, self.train_state.y_pred_test = self.sess.run(
                [self.losses, self.net.outputs],
                feed_dict=self.get_feed_dict(
                    False, False, 1, self.train_state.X_test, self.train_state.y_test
                ),
            )

        if isinstance(self.net.targets, list):
            self.train_state.metrics_test = [
                m(self.train_state.y_test[i], self.train_state.y_pred_test[i])
                for m in self.metrics
                for i in range(len(self.net.targets))
            ]
        else:
            self.train_state.metrics_test = [
                m(self.train_state.y_test, self.train_state.y_pred_test)
                for m in self.metrics
            ]
        self.train_state.update_best()

    def get_feed_dict(self, training, dropout, data_id, inputs, targets):
        feed_dict = {
            self.net.training: training,
            self.net.dropout: dropout,
            self.net.data_id: data_id,
            self.net.inputs: inputs,
        }
        if isinstance(self.net.targets, list):
            feed_dict.update(dict(zip(self.net.targets, targets)))
        else:
            feed_dict.update({self.net.targets: targets})
        return feed_dict

    def get_optimizer(self, name, lr):
        return {
            "sgd": tf.train.GradientDescentOptimizer(lr),
            "sgdnesterov": tf.train.MomentumOptimizer(lr, 0.9, use_nesterov=True),
            "adagrad": tf.train.AdagradOptimizer(0.01),
            "adadelta": tf.train.AdadeltaOptimizer(),
            "rmsprop": tf.train.RMSPropOptimizer(lr),
            "adam": tf.train.AdamOptimizer(lr),
        }[name]

    def get_learningrate(self, lr, decay):
        if decay is None:
            return lr, None
        global_step = tf.Variable(0, trainable=False)
        return (
            {
                "inverse time": tf.train.inverse_time_decay(
                    lr, global_step, decay[1], decay[2]
                ),
                "cosine": tf.train.cosine_decay(
                    lr, global_step, decay[1], alpha=decay[2]
                ),
            }[decay[0]],
            global_step,
        )

    def print_model(self):
        variables_names = [v.name for v in tf.trainable_variables()]
        values = self.sess.run(variables_names)
        for k, v in zip(variables_names, values):
            print("Variable: {}, Shape: {}".format(k, v.shape))
            print(v)


class TrainState(object):
    def __init__(self):
        self.epoch, self.step = 0, 0

        self.sess = None

        # Data
        self.X_train, self.y_train = None, None
        self.X_test, self.y_test = None, None

        # Results of current step
        self.y_pred_train = None
        self.loss_train, self.loss_test = None, None
        self.y_pred_test, self.y_std_test = None, None
        self.metrics_test = None

        # The best results correspond to the min train loss
        self.best_loss_train, self.best_loss_test = np.inf, np.inf
        self.best_y, self.best_ystd = None, None
        self.best_metrics = None

    def update_tfsession(self, sess):
        self.sess = sess

    def update_data_train(self, X_train, y_train):
        self.X_train, self.y_train = X_train, y_train

    def update_data_test(self, X_test, y_test):
        self.X_test, self.y_test = X_test, y_test

    def update_best(self):
        if self.best_loss_train > np.sum(self.loss_train):
            self.best_loss_train = np.sum(self.loss_train)
            self.best_loss_test = np.sum(self.loss_test)
            self.best_y, self.best_ystd = self.y_pred_test, self.y_std_test
            self.best_metrics = self.metrics_test

    def savetxt(self, fname_train, fname_test):
        y_train = self.merge_values(self.y_train)
        y_test = self.merge_values(self.y_test)
        best_y = self.merge_values(self.best_y)
        best_ystd = self.merge_values(self.best_ystd)

        train = np.hstack((self.X_train, y_train))
        np.savetxt(fname_train, train, header="x, y")

        test = np.hstack((self.X_test, y_test, best_y))
        if best_ystd is not None:
            test = np.hstack((test, best_ystd))
        np.savetxt(fname_test, test, header="x, y_true, y_pred, y_std")

    def plot(self):
        y_train = self.merge_values(self.y_train)
        y_test = self.merge_values(self.y_test)
        best_y = self.merge_values(self.best_y)
        best_ystd = self.merge_values(self.best_ystd)

        y_dim = y_train.shape[1]

        plt.figure()
        for i in range(y_dim):
            plt.plot(self.X_train[:, 0], y_train[:, i], "ok", label="Train")
            plt.plot(self.X_test[:, 0], y_test[:, i], "-k", label="True")
            plt.plot(self.X_test[:, 0], best_y[:, i], "--r", label="Prediction")
            if best_ystd is not None:
                plt.plot(
                    self.X_test[:, 0],
                    best_y[:, i] + 2 * best_ystd[:, i],
                    "-b",
                    label="95% CI",
                )
                plt.plot(self.X_test[:, 0], best_y[:, i] - 2 * best_ystd[:, i], "-b")
        plt.xlabel("x")
        plt.ylabel("y")
        plt.legend()

        if best_ystd is not None:
            plt.figure()
            for i in range(y_dim):
                plt.plot(self.X_test[:, 0], best_ystd[:, i], "-b")
                plt.plot(
                    self.X_train[:, 0],
                    np.interp(self.X_train[:, 0], self.X_test[:, 0], best_ystd[:, i]),
                    "ok",
                )
            plt.xlabel("x")
            plt.ylabel("std(y)")

    def merge_values(self, values):
        return np.hstack(values) if isinstance(values, list) else values


class LossHistory(object):
    def __init__(self):
        self.steps = []
        self.loss_train = []
        self.loss_test = []
        self.metrics_test = []
        self.loss_weights = 1

    def update_loss_weights(self, loss_weights):
        self.loss_weights = loss_weights

    def add(self, step, loss_train, loss_test, metrics_test):
        self.steps.append(step)
        self.loss_train.append(loss_train)
        self.loss_test.append(loss_test)
        self.metrics_test.append(metrics_test)

    def savetxt(self, fname):
        loss = np.hstack(
            (
                np.array(self.steps)[:, None],
                np.array(self.loss_train),
                np.array(self.loss_test),
                np.array(self.metrics_test),
            )
        )
        np.savetxt(fname, loss, header="step, loss_train, loss_test, metrics_test")

    def plot(self):
        loss_train = np.sum(np.array(self.loss_train) * self.loss_weights, axis=1)
        loss_test = np.sum(np.array(self.loss_test) * self.loss_weights, axis=1)

        plt.figure()
        plt.semilogy(self.steps, loss_train, label="Train loss")
        plt.semilogy(self.steps, loss_test, label="Test loss")
        for i in range(len(self.metrics_test[0])):
            plt.semilogy(
                self.steps, np.array(self.metrics_test)[:, i], label="Test metric"
            )
        plt.xlabel("# Steps")
        plt.legend()
