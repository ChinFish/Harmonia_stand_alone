#!/usr/bin/env python3

import argparse
import numpy as np
import tensorflow as tf

from utils import uniform_sampler, binary_sampler, normalization
from data_loader import data_loader, map_loader
from models import Discriminator, Generator
from sklearn.metrics import accuracy_score

from concurrent import futures
import logging
import threading
import os
import random
import grpc
import service_pb2
import service_pb2_grpc

OPERATOR_URI = os.getenv('OPERATOR_URI', "localhost:8787")
APPLICATION_URI = "0.0.0.0:7878"
STOP_EVENT = threading.Event()

def get_training_data():
    global __DATA
    if not __DATA:
        __DATA = random.sample(range(60000), 2000)
    return __DATA
    
def train(baseModel, output_model_path, epochs=1):
    data = get_training_data()
    output = os.path.join("/repos", output_model_path, 'weights.tar')
    logging.info(f'input path: [{baseModel.path}]')
    logging.info(f'output path: [{output}]')
    logging.info(f'epochs: {epochs}')

    base_weight_path = os.path.join("/repos", baseModel.path, "weights.tar")
    try:
        metrics = gain(data, out, epochs=1)
    except Exception as err:
        # print('metrics', err)
        logging.debug("metrics ERR : {}".format(err))

    # Send finish message
    logging.info(f"GRPC_CLIENT_URI: {OPERATOR_URI}")
    logging.debug("metrics: {}".format(metrics))
    try:
        channel = grpc.insecure_channel(OPERATOR_URI)
        stub = service_pb2_grpc.EdgeOperatorStub(channel)
        result = service_pb2.LocalTrainResult(
            error=0,
            datasetSize=2500,
            metrics=metrics
        )

        response = stub.LocalTrainFinish(result)
    except grpc.RpcError as rpc_error:
        logging.error("grpc error: {}".format(rpc_error))
    except Exception as err:
        logging.error('got error: {}'.format(err))

    logging.debug("sending grpc message succeeds, response: {}".format(response))

#mnist.train
def gain(ref_pannel, save_model, epochs, batch_size=128, miss_rate=0.9, hint_rate=0.1, size=0, alpha=100):
    logging.info('Start train!')
    
    #----------------------------------GAIN model start----------------------------------
    print('GPU:', tf.test.is_gpu_available())
    
    # Load data to npy
    ori_data_x, miss_data_x, data_m = data_loader(ref_pannel, miss_rate, size)

    # Add and normalize map information
    data_map = map_loader('/content/drive/MyDrive/GAIN/data/TWB_compare/chr22_select_all.map')
    data_map = (data_map - np.min(data_map)) / (np.max(data_map) - np.min(data_map))

    # Define mask matrix (0 denotes missing)
    data_m = 1 - np.isnan(miss_data_x)
    miss_data_x = np.nan_to_num(miss_data_x, 0)

    # Other parameters
    no, dim = miss_data_x.shape

    # Data preprocessing
    norm_data_batch = tf.data.Dataset.from_tensor_slices((ori_data_x, miss_data_x, data_m, ))
    norm_data_batch = norm_data_batch.shuffle(buffer_size=no)
    norm_data_batch = norm_data_batch.batch(batch_size)

    # Define model
    discriminator = Discriminator(int(dim))
    generator = Generator(int(dim))
    D_optimizer = tf.keras.optimizers.Adam(learning_rate=0.001)
    G_optimizer = tf.keras.optimizers.Adam(learning_rate=0.001)
    D_metric = tf.keras.metrics.BinaryAccuracy()
    G_metric = tf.keras.metrics.BinaryAccuracy()

    # Init Loss list
    D_loss_list = []
    G_loss_list = []
    D_acc_list = []
    G_acc_list = []
    G_grad = []
    G_lr = []

    # Training loop
    for epoch in range(epochs):
        for step, (O_mb, X_mb, M_mb) in enumerate(norm_data_batch):
            Map_mb = np.repeat(data_map[np.newaxis, :], X_mb.shape[0], axis=0)
            Map_mb = tf.cast(Map_mb, dtype=tf.float32)

            X_mb = tf.cast(X_mb, dtype=tf.float32)
            M_mb = tf.cast(M_mb, dtype=tf.float32)
            Z_mb = uniform_sampler(0, 0.01, tf.shape(M_mb)[0], dim)

            X_mb = X_mb + (1 - M_mb) * Z_mb

            # G_sample = generator([X_mb, M_mb, Map_mb], training=False)
            G_sample = generator([X_mb, M_mb], training=False)
            Hat_X = X_mb + G_sample * (1 - X_mb)

            H_mb_temp = binary_sampler(hint_rate, tf.shape(M_mb)[0], dim)
            H_mb = M_mb * tf.convert_to_tensor(H_mb_temp, dtype=np.float32)

            with tf.GradientTape() as tape:
                D_prob = discriminator([Hat_X, H_mb], training=True)
                D_loss = -tf.reduce_mean(M_mb * tf.math.log(D_prob + 1e-8) + (1 - M_mb)
                                        * tf.math.log(1. - D_prob + 1e-8))
            D_metric.update_state(M_mb, D_prob)
            D_acc = D_metric.result().numpy()
            # D_acc = accuracy_score(M_mb, D_prob)

            grads = tape.gradient(D_loss, discriminator.trainable_weights)
            D_optimizer.apply_gradients(zip(grads, discriminator.trainable_weights))

            with tf.GradientTape() as tape:
                # G_sample = generator([X_mb, M_mb, Map_mb], training=True)
                G_sample = generator([X_mb, M_mb], training=True)

                G_loss_temp = -tf.reduce_mean((1 - M_mb) * tf.math.log(D_prob + 1e-8))
                MSE_loss = tf.reduce_mean((M_mb * X_mb - M_mb * G_sample)**2) / tf.reduce_mean(M_mb)
                G_loss = G_loss_temp + alpha * MSE_loss
            G_metric.update_state(O_mb, G_sample)
            G_acc = G_metric.result().numpy()
            # G_acc = accuracy_score(O_mb[M_mb==1], tf.round(G_sample[M_mb==1]))

            grads = tape.gradient(G_loss, generator.trainable_weights)
            G_optimizer.apply_gradients(zip(grads, generator.trainable_weights))

            # Recored Loss, Acc., Gradient
            D_loss_list.append(D_loss)
            G_loss_list.append(G_loss)
            D_acc_list.append(D_acc)
            G_acc_list.append(G_acc)
            G_lr.append(G_optimizer._decayed_lr('float32').numpy())
            curr_grad = []
            for i in grads:
                curr_grad.append(np.linalg.norm(np.array(i)))
            G_grad.append(curr_grad)

            # Verbose
                metrics={}
            if step % 10 == 0:
                metrics = {'D_loss': D_loss,'accuracy': D_accuracy}
                print('Epoch:{:3d}\tSteps:{:3d}\tD_loss:{:.3g}\tG_loss:{:.3g}\tD_accuracy:{:.3g}\tG_accuracy:{:.3g}'.format(
                    epoch, step, D_loss, G_loss, D_acc, G_acc))
    # Save model
    # discriminator.save("./model/%s_D" % (save_model))
    # generator.save("./model/%s_G" % (save_model))
    # tf.saved_model.save(discriminator, "./model/%s_D" % (save_model))
    # tf.saved_model.save(generator, "./model/%s_G" % (save_model))

    # Save loss
    # D_loss_list = np.array(D_loss_list)
    # G_loss_list = np.array(G_loss_list)
    # D_acc = np.array(D_acc)
    # G_acc = np.array(G_acc)
    # G_lr = np.array(G_lr)
    # G_grad = np.array(G_grad)
    # np.save("./model/%s_D_loss.npy" % (save_model), D_loss_list)
    # np.save("./model/%s_G_loss.npy" % (save_model), G_loss_list)
    # np.save("./model/%s_G_lr.npy" % (save_model), G_lr)
    # np.save("./model/%s_G_grad.npy" % (save_model), G_grad)
    #----------------------------------GAIN model END----------------------------------
    return metrics
    
class EdgeAppServicer(service_pb2_grpc.EdgeAppServicer):
    def TrainInit(self, request, context):
        logging.info("TrainInit")
        resp = service_pb2.Empty()
        logging.info(f"Sending response: {resp}")
        return resp

    def LocalTrain(self, request, context):
        logging.info("LocalTrain")

        threading.Thread(
            target=train,
            args=(request.baseModel, request.localModel.path, request.EpR),
            daemon=True
        ).start()

        resp = service_pb2.Empty()
        logging.info("Sending response: {}".format(resp))
        return resp

    def TrainInterrupt(self, _request, _context):
        # Not Implemented
        return service_pb2.Empty()

    def TrainFinish(self, _request, _context):
        logging.info("TrainFinish")
        STOP_EVENT.set()
        return service_pb2.Empty()    
        
def serve():
    logging.basicConfig(level=logging.DEBUG)

    logging.info("Start server... {}".format(APPLICATION_URI))

    if(tf.test.is_gpu_available()):
        logging.info("GPU: True")
    else:
        logging.info("GPU: False")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    service_pb2_grpc.add_EdgeAppServicer_to_server(
        EdgeAppServicer(), server)
    server.add_insecure_port(APPLICATION_URI)
    server.start()

    STOP_EVENT.wait()
    logging.info("Server Stop")
    server.stop(None)

if __name__ == "__main__":
    serve()