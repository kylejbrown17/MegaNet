
import argparse
import os
from pycocotools.coco import COCO
import numpy as np
import random
import skimage.io as io
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import pylab
import tensorflow as tf
import tensorflow.contrib.slim as slim
import tensorflow.contrib.slim.nets
# from tensorflow.contrib.slim.python.slim.nets import resnet_v2
from tensorflow.contrib.layers.python.layers import utils

pylab.rcParams['figure.figsize'] = (10.0, 8.0)


parser = argparse.ArgumentParser()
parser.add_argument('--base_dir', default='/home/kyle/Project/coco')
parser.add_argument('--train_data', default='person_keypoints_train2014')
parser.add_argument('--val_data', default='person_keypoints_val2014')
parser.add_argument('--test_data', default='image_info_test-dev2015')
parser.add_argument('--image_train_dir', default='train2014')
parser.add_argument('--image_val_dir', default='val2014')
parser.add_argument('--image_test_dir', default='test2015')

parser.add_argument('--model_path', default='/home/kyle/MegaNet/checkpoints/resnet_v2_50.ckpt', type=str)
parser.add_argument('--batch_size', default=10, type=int)
parser.add_argument('--num_workers', default=4, type=int)
parser.add_argument('--num_epochs1', default=10, type=int)
parser.add_argument('--num_epochs2', default=10, type=int)
parser.add_argument('--learning_rate1', default=1e-3, type=float)
parser.add_argument('--learning_rate2', default=1e-5, type=float)
parser.add_argument('--dropout_keep_prob', default=0.5, type=float)
parser.add_argument('--weight_decay', default=5e-4, type=float)

HEAD_SCOPE = 'Head'

def keypoint_CrossEntropyLoss(graph, prediction_maps, keypoint_masks, labels, L=5.0, scope="keypointLoss"):
    """
    heat_maps = predictions from network
    keypoints (N,17,2) = actual keypoint locations
    labels (N,17,1) = 0 if invalid, 1 if occluded, 2 if valid
    """
    with graph.as_default():
        losses = tf.nn.sigmoid_cross_entropy_with_logits(logits=prediction_maps,labels=keypoint_masks)
        labels = tf.reshape(labels,[-1,1,1,17])
        losses = tf.multiply(losses,labels) # set loss to zero for invalid keypoints (labels=0)
        
        return losses


def keypoint_SquaredErrorLoss(graph, prediction_maps, keypoint_masks, labels, L=5.0, scope="keypointLoss"):
    """
    heat_maps = predictions from network
    keypoints (N,17,2) = actual keypoint locations
    labels (N,17,1) = 0 if invalid, 1 if occluded, 2 if valid
    """
    with graph.as_default():
        with tf.variable_scope(scope):
            losses = tf.squared_difference(prediction_maps,keypoint_masks)
            labels = tf.reshape(labels,[-1,1,1,17])
            losses = tf.multiply(losses,labels) # set loss to zero for invalid keypoints (labels=0)
            
            return losses

def KeypointPrediction(graph, pred_masks, d, scope='KeypointPrediction'):
    """
    Input: Keypoint "Heatmap" Tensor
    Output: Keypoint coordinates in tensor form
    """
    with graph.as_default():        
        with tf.variable_scope(scope):
            x = tf.reshape(tf.linspace(0.5,d-0.5,d),[1,d,1,1])
            pred = tf.multiply(pred_masks, tf.to_float(tf.greater_equal(pred_masks,0.5)))
            pred_i = tf.reduce_sum(tf.multiply(pred, x),axis=[1,2])/tf.reduce_sum(pred,axis=[1,2])
            pred_j = tf.reduce_sum(tf.multiply(pred, tf.transpose(x,(0,2,1,3))),axis=[1,2])/tf.reduce_sum(pred,axis=[1,2])
            pred_pts = tf.stack([pred_j,pred_i],axis=1)
            pred_pts = tf.expand_dims(pred_pts,axis=1)
            return pred_pts

def keypointPredictionAccuracy(graph, pred_pts, true_pts, labels, threshold, scope='KeypointPrediction'):
    """
    Accuracy is a boolean: 1 if ||pred_pt-true_pt||^2 < threshold^2, 0 otherwise
    """
    with graph.as_default():
        with tf.variable_scope(scope):
            error = tf.multiply(tf.square(tf.subtract(pred_pts, true_pts)), tf.to_float(tf.greater_equal(labels, 1)))
            accuracy = tf.reduce_mean(tf.to_float(tf.less(error,tf.square(threshold))))
            return accuracy

def MaskAccuracy(graph, pred_mask, true_mask):
    with graph.as_default():        
        overlap = tf.reduce_sum(tf.multiply(tf.to_float(pred_mask),tf.to_float(true_mask)),axis=[1,2,3])
        score1 = tf.divide(overlap, tf.reduce_sum(tf.to_float(pred_mask),axis=[1,2,3]))
        score2 = tf.divide(overlap, tf.reduce_sum(tf.to_float(true_mask),axis=[1,2,3]))
        accuracy = tf.minimum(score1,score2)
        return tf.reduce_mean(accuracy)


# Initialize Dataset
def get_data(base_dir,image_dir,ann_file):
    image_path = '{}/images/{}'.format(base_dir,image_dir)
    ann_path='{}/annotations/{}.json'.format(base_dir,ann_file)

    return image_path, ann_path
    
# define the path to the annotation file corresponding to the images you want to work with

def main(args):
    ######################## Data Path ########################
    # baseDir = args.base_dir
    # trainData = args.train_data
    # valData = args.val_data
    # testData = args.test_data
    # imageTrainDir = args.image_train_dir
    # imageValDir = args.image_val_dir
    # imageTestDir = args.image_test_dir

    train_img_path, train_ann_path = get_data(args.base_dir,args.image_train_dir,args.train_data)
    val_img_path, val_ann_path = get_data(args.base_dir,args.image_val_dir,args.val_data)
    # initialize a coco object
    print("Initializing COCO object to extract dataset...\n")
    coco = COCO(train_ann_path)
    # get all images containing the 'person' category
    catIds = coco.getCatIds(catNms=['person'])
    imgIds = coco.getImgIds(catIds=catIds)
    # Just for dealing with the images on my computer (not necessary when working with the whole dataset)
    # catIds = imgIds[0:30]
    # imgIds = imgIds[0:30]

    graph = tf.Graph()
    with graph.as_default():
        
        #######################################################
        ############### VARIOUS HYPER-PARAMETERS ##############
        #######################################################

        NUM_KEYPOINTS = 17
        BATCH_SIZE = 10
        L = 5.0 # keypoint effective radius
        D = 225 # image height and width
        d = 57 # evaluation height and width (for mask and keypoint masks)

        MASK_THRESHOLD = 0.5 # threshold for on/off prediction (in mask and keypoint masks)
        KP_THRESHOLD = 0.5 # threshold for on/off prediction (in mask and keypoint masks)
        KP_DISTANCE_THRESHOLD = 5.0 # threshold for determining if a keypoint estimate is accurate
        X_INIT = tf.contrib.layers.xavier_initializer_conv2d() # xavier initializer for head architecture
        learning_rate1 = args.learning_rate1
        learning_rate2 = args.learning_rate2

        #######################################################
        #### VISUALIZATION TOOLS - WEIGHTS AND ACTIVATIONS ####
        #######################################################
        def highestPrimeFactorization(n):    
            return [(i, n//i) for i in range(1, int(n**0.5) + 1) if n % i == 0][-1] 

        def getFilterImage(filters):
            """
            Takes as input a filter bank of size (1, H, W, C, D)
            Returns: a tensor of size (1, sqrt(D)*H, sqrt(D)*H, C)
            (This only really works for the first layer of filtes with an image as input)
            """
            padded_filters = tf.pad(filters,tf.constant([[0,0],[1,0],[1,0],[0,0],[0,0]]),'CONSTANT')
            filter_list = tf.unstack(padded_filters,axis=4)
            H,W = highestPrimeFactorization(len(filter_list))
            weight_strips = [tf.concat(filter_list[8*i:8*(i+1)],axis=1) for i in range(W)]
            weight_image = tf.concat(weight_strips,axis=2)
            return weight_image
    
        def getActivationImage(activations):
            """
            Tiles an activation map into a square grayscale image
            Takes as input an activation map of size (N, H, W, D)
            Returns: a tensor of size (N, sqrt(D)*H, sqrt(D)*H, 1)
            """
            padded_activations = tf.pad(activations,tf.constant([[0,0],[1,0],[1,0],[0,0]]),'CONSTANT')
            expanded_activations = tf.expand_dims(padded_activations,axis=3)
            activations_list = tf.unstack(expanded_activations,axis=4)
            H,W = highestPrimeFactorization(len(activations_list))
            activation_strips = [tf.concat(activations_list[H*i:H*(i+1)],axis=1) for i in range(W)]
            activation_image = tf.concat(activation_strips,axis=2)
            return activation_image
        #######################################################
        ##### PRE-PROCESSING AND DATASET EXTRACTION TOOLS #####
        #######################################################
        def extract_annotations(filename, imgID, coco=coco):
            anns = coco.loadAnns(coco.getAnnIds(imgID,catIds=[1],iscrowd=None))
            ann = max([ann for ann in anns], key=lambda item:item['area']) # extract annotation for biggest instance
            bbox = np.array(np.floor(ann['bbox']),dtype=int)
            keypoints = np.reshape(ann['keypoints'],(-1,3))
            mask = coco.annToMask(ann)
            
            return filename, bbox, keypoints, mask
        
        def preprocess_image_tf(filename, bbox_tensor, keypoints_tensor, mask, D=D):
            """
            Returns:
            resized_image (N,D,D,3) - cropped, padded (if needed), scaled to square image of size D
            resized_mask (N,D,D,1) - cropped, padded (if needed), scaled to square mask of size D
            pts (N,2,17) - keypoint coordinates (i,j) scaled to match up with resized_image
            labels (N,1,17) - values corresponding to pts: {0: invalid, 1:occluded, 2:valid}
            """
            image_string = tf.read_file(filename)
            image_decoded = tf.image.decode_jpeg(image_string, channels=3)
            image = tf.cast(image_decoded, tf.float32)

            mask = tf.transpose([mask],[1,2,0])
            bbox_tensor = tf.to_float(bbox_tensor)
            keypoints_tensor = tf.to_float(keypoints_tensor)

            sideLength = tf.reduce_max(bbox_tensor[2:],axis=0)
            centerX = tf.floor(bbox_tensor[0] + tf.divide(bbox_tensor[2],tf.constant(2.0)))
            centerY = tf.floor(bbox_tensor[1] + tf.divide(bbox_tensor[3],tf.constant(2.0)))
            center = tf.stack([centerX,centerY])

            corner1 = tf.to_int32(tf.minimum(tf.maximum(tf.subtract(center, tf.divide(sideLength,tf.constant(2.0))),0),
                                tf.reverse(tf.to_float(tf.shape(image)[:2]),tf.constant([0]))))
            corner2 = tf.to_int32(tf.minimum(tf.maximum(tf.add(center, tf.divide(sideLength,tf.constant(2.0))),0),
                                tf.reverse(tf.to_float(tf.shape(image)[:2]),tf.constant([0]))))
            i_shape = tf.subtract(corner2,corner1)
            d_shape = tf.subtract(tf.to_int32(sideLength),i_shape)

            scale = tf.divide(tf.constant(D,tf.float32), sideLength)
            cropped_image = tf.image.crop_to_bounding_box(image,corner1[1],corner1[0],
                                                        tf.subtract(corner2,corner1)[1],tf.subtract(corner2,corner1)[0])
            cropped_mask = tf.image.crop_to_bounding_box(mask,corner1[1],corner1[0],
                                                        tf.subtract(corner2,corner1)[1],tf.subtract(corner2,corner1)[0])

            dX = tf.floor(tf.divide(d_shape,tf.constant(2)))
            dY = tf.ceil(tf.divide(d_shape,tf.constant(2)))

            pts, labels = tf.split(keypoints_tensor,[2,1],axis=1)
            pts = tf.subtract(pts,tf.to_float(corner1)) # shift keypoints
            pts = tf.add(pts,tf.to_float(dX)) # shift keypoints
            pts = tf.multiply(pts,scale) # scale keypoints

            # set invalid pts to 0
            inbounds = tf.less(pts,tf.constant(D,tf.float32))
            inbounds = tf.multiply(tf.to_int32(inbounds), tf.to_int32(tf.greater(pts,0)))
            pts = tf.multiply(pts,tf.to_float(inbounds))
            pts = tf.transpose(pts,[1,0])
            labels = tf.transpose(labels,[1,0])

            padded_image = tf.image.pad_to_bounding_box(cropped_image,tf.to_int32(dX[1]),tf.to_int32(dX[0]),
                                                        tf.to_int32(sideLength),tf.to_int32(sideLength))
            padded_mask = tf.image.pad_to_bounding_box(cropped_mask,tf.to_int32(dX[1]),tf.to_int32(dX[0]),
                                                        tf.to_int32(sideLength),tf.to_int32(sideLength))

            resized_image = tf.image.resize_images(padded_image,tf.constant([D,D]),tf.image.ResizeMethod.NEAREST_NEIGHBOR)
            # resized_image = resized_image - VGG_MEAN
            resized_mask = tf.image.resize_images(padded_mask,tf.constant([D,D]),tf.image.ResizeMethod.NEAREST_NEIGHBOR)
            return resized_image, resized_mask, pts, labels

        def scaleDownMaskAndKeypoints(image, mask, pts, labels, d=d, D=D):
            mask = tf.image.resize_images(mask,tf.constant([d,d]),tf.image.ResizeMethod.NEAREST_NEIGHBOR)
            pts = tf.multiply(pts,tf.constant(d/D))
            return image, mask, pts, labels
        
        def generate_keypoint_masks(image, mask, keypoints, labels, d=d, D=D, L=L):
            X, Y = tf.meshgrid(tf.linspace(0.0,d,d),tf.linspace(0.0,d,d))
            X = tf.reshape(X,[d,d,1])
            Y = tf.reshape(Y,[d,d,1])
            X_stack = tf.tile(X,tf.constant([1,1,17],dtype=tf.int32))
            Y_stack = tf.tile(Y,tf.constant([1,1,17],dtype=tf.int32))

            pts = tf.reshape(keypoints,[1,2,17])
            ptsX, ptsY = tf.split(pts,[1,1],axis=1)
            d1 = tf.square(tf.subtract(X_stack,ptsX))
            d2 = tf.square(tf.subtract(Y_stack,ptsY))

            pt_masks = tf.multiply(tf.divide(tf.constant(1.0),tf.add(d1,d2)+L),L)
            return image, mask, pt_masks, pts, labels
        
        #######################################################
        ################## SUMMARY DICTIONARY #################
        #######################################################

        image_summary_list = []
        scalar_summary_list = []

        #######################################################
        ################### PREPARE DATASET ###################
        #######################################################
        print("Initializing Dataset...\n")
        with tf.variable_scope('DataSet'):
            # Initialize train_dataset
            filenames = tf.constant(['{}/COCO_train2014_{:0>12}.jpg'.format(train_img_path,imgID) for imgID in imgIds])
            imgID_tensor = tf.constant(imgIds)
            train_dataset = tf.contrib.data.Dataset.from_tensor_slices((filenames,imgID_tensor))
            # Extract Annotations via coco interface
            train_dataset = train_dataset.map(lambda filename, imgID: tf.py_func(extract_annotations, [filename, imgID], 
                                                                        [filename.dtype, tf.int64, tf.int64, tf.uint8]))
            # All other preprocessing in tensorflow
            train_dataset = train_dataset.map(preprocess_image_tf)
            train_dataset = train_dataset.map(scaleDownMaskAndKeypoints)
            train_dataset = train_dataset.map(generate_keypoint_masks)
            # BATCH
            train_dataset = train_dataset.shuffle(buffer_size=10000)
            train_dataset = train_dataset.batch(BATCH_SIZE) # must resize images to make them match
            iterator = tf.contrib.data.Iterator.from_structure(train_dataset.output_types,train_dataset.output_shapes)

            # images: (N,225,225,3), masks: (N,57,57,1), kpt_masks: (N,17,57,57,1), pts: (N,1,2,17), labels: (N,1,1,17)
            images, masks, kpt_masks, pts, labels = iterator.get_next()
            train_init_op = iterator.make_initializer(train_dataset)

            image_summary_list.append(tf.summary.image('keypoint masks', getActivationImage(kpt_masks)))
            image_summary_list.append(tf.summary.image('input images', images))
            # summary_dict['DataSet']['images']['keypoint_masks'] = tf.summary.image('keypoint masks', getActivationImage(kpt_masks))
            # summary_dict['dataset']['images']['images'] = tf.summary.image('input images', images)
        
        #######################################################
        ##################### BUILD GRAPH #####################
        #######################################################

        is_training = tf.placeholder(tf.bool)

        # --------------------------------------------------- #
        # ------------- Resnet V2 50 "Backbone" ------------- #
        # --------------------------------------------------- #
        print("Loading ResNet V2 50 Backbone architecture...")
        resnet_v2 = tf.contrib.slim.nets.resnet_v2
        with slim.arg_scope(resnet_v2.resnet_arg_scope()):
            logits, endpoints = resnet_v2.resnet_v2_50(
                inputs=images,
                num_classes=10,
                is_training=is_training,
                reuse=None,
                output_stride=16,
                scope='resnet_v2_50'
                )

        # Model Path to ResNet v2 50 checkpoint
        model_path = args.model_path
        assert(os.path.isfile(model_path))
        # Backbone Variables - remember to exclude all variables above backbone (including block4 and logits)
        backbone_variables = tf.contrib.framework.get_variables_to_restore(exclude=['resnet_v2_50/postnorm','resnet_v2_50/logits'])
        init_fn = tf.contrib.framework.assign_from_checkpoint_fn(model_path, backbone_variables) # Call to load pretrained weights

        with tf.name_scope('ResNet'):
            image_summary_list.append(tf.summary.image(
                'ResNet - layer1 weights',getFilterImage(tf.contrib.framework.get_variables('resnet_v2_50/conv1/weights'))))
            for i in range(4):
                image_summary_list.append(tf.summary.image(
                    'ResNet - block {}'.format(i+1), getActivationImage(endpoints['resnet_v2_50/block{}'.format(i+1)])
                    ))

        # --------------------------------------------------- #
        # --------------- "Head" Architecture --------------- #
        # --------------------------------------------------- #
        print("Defining Network Head architecture...\n")
        block1 = endpoints['resnet_v2_50/block1']
        block2 = endpoints['resnet_v2_50/block2']
        block3 = endpoints['resnet_v2_50/block3']
        block4 = endpoints['resnet_v2_50/block4']

        HEAD_SCOPE = 'NetworkHead'

        with tf.variable_scope(HEAD_SCOPE):
            with tf.variable_scope('Layer1'):
                b1 = tf.layers.conv2d(block1, 64, kernel_size=(3,3), strides=(1,1),padding='SAME',activation=tf.nn.relu, kernel_initializer=X_INIT)
                b2 = tf.layers.conv2d(block2, 128, kernel_size=(3,3), strides=(1,1),padding='SAME',activation=tf.nn.relu, kernel_initializer=X_INIT)
                b3 = tf.layers.conv2d(block3, 128, kernel_size=(1,1), strides=(1,1),padding='SAME',activation=tf.nn.relu, kernel_initializer=X_INIT)
                b4 = tf.layers.conv2d(block4, 128, kernel_size=(1,1), strides=(1,1),padding='SAME',activation=tf.nn.relu, kernel_initializer=X_INIT)

                image_summary_list.append(tf.summary.image('Head - b1', getActivationImage(b1)))
                image_summary_list.append(tf.summary.image('Head - b2', getActivationImage(b2)))
                image_summary_list.append(tf.summary.image('Head - b3', getActivationImage(b3)))
                image_summary_list.append(tf.summary.image('Head - b4', getActivationImage(b4)))

            with tf.variable_scope('Layer2'):
                b1 = tf.layers.conv2d(block1, 32, kernel_size=(3,3), strides=(1,1),padding='SAME',activation=tf.nn.relu, kernel_initializer=X_INIT)

                b2 = tf.layers.conv2d_transpose(b2, 32, kernel_size=(3,3), strides=(2,2),padding='VALID',activation=tf.nn.relu, kernel_initializer=X_INIT)
                b3 = tf.layers.conv2d_transpose(b3, 64, kernel_size=(3,3), strides=(2,2),padding='VALID',activation=tf.nn.relu, kernel_initializer=X_INIT)
                b4 = tf.layers.conv2d_transpose(b4, 64, kernel_size=(3,3), strides=(2,2),padding='VALID',activation=tf.nn.relu, kernel_initializer=X_INIT)
                # Crop back down to 29x29
                b2 = b2[:,1:-1,1:-1,:]
                b3 = b3[:,1:-1,1:-1,:]
                b4 = b4[:,1:-1,1:-1,:]

            with tf.variable_scope('BatchNorm'):
                b1 = tf.layers.batch_normalization(b1)
                b2 = tf.layers.batch_normalization(b2)
                b3 = tf.layers.batch_normalization(b3)
                b4 = tf.layers.batch_normalization(b4)

            with tf.variable_scope('Funnel'):
                head = tf.concat([b1,b2,b3,b4],axis=3)
                head = tf.layers.conv2d(head,64,(1,1),(1,1),'SAME',activation=tf.nn.relu,kernel_initializer=X_INIT)

            with tf.variable_scope('MaskHead'):
                mask_head = tf.layers.conv2d_transpose(head, 32, (3,3), (2,2), padding='VALID', activation=tf.nn.relu, kernel_initializer=X_INIT)
                mask_head = mask_head[:,1:-1,1:-1,:]
                mask_head = tf.layers.conv2d(mask_head, 16, (3,3), (1,1), padding='SAME', activation=None, kernel_initializer=X_INIT)
                mask_head = tf.layers.conv2d(mask_head, 1, (1,1), (1,1), padding='SAME', activation=None, kernel_initializer=X_INIT)

            with tf.variable_scope('KeypointHead'):
                keypoint_head = tf.layers.conv2d_transpose(head, 32, (3,3), (2,2), padding='VALID', activation=tf.nn.relu, kernel_initializer=X_INIT)
                keypoint_head = keypoint_head[:,1:-1,1:-1,:]
                keypoint_head = tf.layers.conv2d(keypoint_head, 32, (3,3), (1,1), padding='SAME', activation=None, kernel_initializer=X_INIT)
                keypoint_head = tf.layers.conv2d(keypoint_head, 17, (1,1), (1,1), padding='SAME', activation=None, kernel_initializer=X_INIT)

        ########## Prediction and Accuracy Checking ########### 

            with tf.variable_scope('MaskPrediction'):
                mask_prediction = tf.nn.sigmoid(mask_head)
                mask_prediction = tf.to_float(tf.greater_equal(mask_prediction, MASK_THRESHOLD))
                mask_accuracy = MaskAccuracy(graph, mask_prediction, masks)

                image_summary_list.append(tf.summary.image('Head - mask prediction', mask_prediction))
                scalar_summary_list.append(tf.summary.scalar('Head - mask accuracy', mask_accuracy))

            with tf.variable_scope('KeypointsPrediction'):
                keypoint_mask_prediction = tf.nn.sigmoid(keypoint_head)
                keypoint_mask_prediction = tf.to_float(tf.greater_equal(keypoint_mask_prediction, KP_THRESHOLD))
                keypoint_prediction = KeypointPrediction(graph, keypoint_mask_prediction, d=d)

                image_summary_list.append(tf.summary.image('Head - keypoint mask prediction', getActivationImage(keypoint_mask_prediction)))
                for i in [1.0,2.0,3.0,5.0,8.0]:
                    scalar_summary_list.append(tf.summary.scalar(
                        'Head - keypoint accuracy delta={}'.format(i), keypointPredictionAccuracy(graph, keypoint_prediction, pts, labels, i)
                    ))
                
                
        #######################################################
        ####################### LOSSES ########################
        #######################################################

            with tf.variable_scope('Losses'):
                with tf.variable_scope('SegmentationLoss'):
                    mask_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=mask_head, labels=tf.to_float(masks)))
                with tf.variable_scope('KeypointLoss'):
                    keypoint_loss = tf.reduce_mean(keypoint_CrossEntropyLoss(graph, keypoint_head, kpt_masks, labels, L=L))
            
                # mask_loss_summary = tf.summary.scalar('MaskLoss', mask_loss)
                scalar_summary_list.append(tf.summary.scalar('Head - mask loss', mask_loss, collections=None))
                # keypoint_loss_summary = tf.summary.scalar('KeypointLoss', keypoint_loss)
                scalar_summary_list.append(tf.summary.scalar('Head - keypoint loss', keypoint_loss, collections=None))

                total_loss = tf.add_n([mask_loss, keypoint_loss],name='TotalLoss')

        # Call to initialize Head Variables from scratch
        head_variables = tf.contrib.framework.get_variables(HEAD_SCOPE)
        init_head = tf.variables_initializer(head_variables, 'init_head')

        # all_variables = tf.contrib.framework.get_trainable_variables() # backbone AND head

        #######################################################
        ###################### OPTIMIZERS #####################
        #######################################################

        with tf.variable_scope('Optimizers'):
            head_optimizer = tf.train.RMSPropOptimizer(learning_rate1)
            head_train_op = head_optimizer.minimize(total_loss, global_step=None, var_list=head_variables, gate_gradients=tf.train.RMSPropOptimizer.GATE_NONE)
        
        # RMSProp optimizer uses "slot" variables for maintaining the running average of weight updates. It must therefore be initialized 
        optimizer_variables = tf.contrib.framework.get_variables('Optimizers')
        init_optimizer = tf.variables_initializer(optimizer_variables)

        #######################################################
        ###################### SUMMARIES ######################
        #######################################################

        image_summary = tf.summary.merge(image_summary_list, collections=None, name="Image Summaries")
        scalar_summary = tf.summary.merge(scalar_summary_list, collections=None, name="Scalar Summaries")

        # Finalize default graph - THIS SEEMS TO PREVENT ADDING A FILEWRITER LATER
        tf.get_default_graph().finalize()

        # Train!
        with tf.Session(graph=graph) as sess:
            # file writer to save graph for Tensorboard
            file_writer = tf.summary.FileWriter('/tmp/KyleNet/1')
            file_writer.add_graph(sess.graph)
            # initialize variables
            print("Initializing backbone variables...")
            init_fn(sess) # pretrained backbone variables
            print("Initializing head variables...")
            sess.run(init_head) # head variables
            print("Initializing optimizer variables...\n")
            sess.run(init_optimizer)
            # initialize dataset iterator
            # sess.run(train_init_op)

            print("Beginning Training...")
            for epoch in range(args.num_epochs1):
                # Run an epoch over the training data.
                print('### Starting epoch {}/{} ####################'.format(epoch + 1, args.num_epochs1))
                sess.run(train_init_op) # initialize the iterator with the training set.

                batch = 1
                while True:
                    try:
                        total_loss_val, _ = sess.run([total_loss, head_train_op], {is_training: True})
                        print('----- Total loss for batch {}: {}'.format(batch, total_loss_val))
                        batch += 1
                    except tf.errors.OutOfRangeError:
                        break

                # reinitialize dataset to run accuracy checks and generate summaries for visualization in Tensorboard
                sess.run(train_init_op)
                if epoch % 200 == 0:
                    image_summ, scalar_summ = sess.run([image_summary, scalar_summary],{is_training: False})
                    file_writer.add_summary(image_summ, global_step=epoch)
                    file_writer.add_summary(scalar_summ, global_step=epoch)
                else:
                    scalar_summ = sess.run(scalar_summary, {is_training: False})
                    file_writer.add_summary(scalar_summ, global_step=epoch)


                # # Check accuracy on the train and val sets every epoch.
                # train_acc = check_accuracy(sess, correct_prediction, is_training, train_init_op)
                # val_acc = check_accuracy(sess, correct_prediction, is_training, val_init_op)
                # print('Train accuracy: %f' % train_acc)
                # print('Val accuracy: %f\n' % val_acc)


            # run some summaries to look at in tensorboard
            # visualization_summaries = sess.run(image_summary_list,{is_training: False})
            # file_writer.add_summary(visualization_summaries, global_step=1)

            # keypoint_masks = sess.run(kpt_masks, {is_training: False})

            # plt.figure()
            # plt.imshow(keypoint_masks[0][:,:,0])
            # plt.show()

            print("Finished")
            return



if __name__ == "__main__":
    args = parser.parse_args()
    main(args)
