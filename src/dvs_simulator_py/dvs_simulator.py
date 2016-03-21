#!/usr/bin/env python

import rospy
import rospkg
import rosbag
import OpenEXR
import numpy as np
import os.path
import time
import cv2
import math

from dvs_simulator_py import dataset_utils
from dvs_simulator_py import extract_motion_field as mf
from std_msgs.msg import Float32, Int16
from dvs_msgs.msg import Event, EventArray
from geometry_msgs.msg import PoseStamped, TwistStamped
from sensor_msgs.msg import CameraInfo
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError


class DvsSimulator:
    
    def __init__(self, initial_time, initial_values, C):
        assert(C > 0)
        self.C = C
        assert(initial_values.shape[0] > 0)
        assert(initial_values.shape[1] > 0)
        self.height = initial_values.shape[0]
        self.width = initial_values.shape[1]
        self.reference_values = initial_values.copy()
        self.It_array = initial_values.copy()
        self.t = initial_time
        
    def update(self, t_dt, It_dt_array):
        
        assert(It_dt_array.shape == self.It_array.shape)

        delta_t = t_dt-self.t
        assert(delta_t > 0)
        
        current_events = []
        for u in range(self.width):
            for v in range(self.height):
                events_for_px = []
                It = self.It_array[v,u]
                It_dt = It_dt_array[v,u]
                previous_crossing = self.reference_values[v,u]
                
                
                tol = 1e-6
                if math.fabs(It-It_dt) > tol: 
                    
                    polarity = +1 if It_dt >= It else -1
                    
                    list_crossings = []
                    all_crossings_found = False
                    cur_crossing = previous_crossing
                    while not all_crossings_found:
                        cur_crossing += polarity * self.C
                        if polarity > 0:
                            if cur_crossing > It and cur_crossing <= It_dt:
                                list_crossings.append(cur_crossing)
                            else:
                                all_crossings_found = True
                        else:
                            if cur_crossing < It and cur_crossing >= It_dt:
                                list_crossings.append(cur_crossing)
                            else:
                                all_crossings_found = True
                                
                    for crossing in list_crossings:
                        te = self.t + (crossing-It) * delta_t / (It_dt-It)
                        events_for_px.append(make_event(u,v,te,polarity>0))
                        
                    current_events += events_for_px
                        
                    if bool(list_crossings):
                        self.reference_values[v,u] = list_crossings[-1]
        
        self.It_array = It_dt_array.copy()
        self.t = t_dt
        
        return current_events


def make_camera_msg(cam):
    camera_info_msg = CameraInfo()
    width, height = cam.width, cam.height
    fx, fy = cam.focal_length
    cx, cy = cam.principle_point
    camera_info_msg.width = width
    camera_info_msg.height = height
    camera_info_msg.K = [fx, 0, cx,
                         0, fy, cy,
                         0, 0, 1]
                         
    camera_info_msg.D = [0, 0, 0, 0]
    
    camera_info_msg.P = [fx, 0, cx, 0,
                         0, fy, cy, 0,
                         0, 0, 1, 0]
    return camera_info_msg
    
    
    
def make_pose_msg(position, orientation, timestamp):
    pose_msg = PoseStamped()
    pose_msg.header.stamp = timestamp
    pose_msg.header.frame_id = '/dvs_simulator'
    pose_msg.pose.position.x = position[0]
    pose_msg.pose.position.y = position[1]
    pose_msg.pose.position.z = position[2]
    pose_msg.pose.orientation.x = orientation[0]
    pose_msg.pose.orientation.y = orientation[1]
    pose_msg.pose.orientation.z = orientation[2]
    pose_msg.pose.orientation.w = orientation[3]
    return pose_msg


def make_twist_msg(lin_vel, ang_vel, timestamp):
    twist_msg = TwistStamped()
    twist_msg.header.stamp = timestamp
    twist_msg.header.frame_id = '/dvs_simulator'
    twist_msg.twist.linear.x = lin_vel[0]
    twist_msg.twist.linear.y = lin_vel[1]
    twist_msg.twist.linear.z = lin_vel[2]
    twist_msg.twist.angular.x = ang_vel[0]
    twist_msg.twist.angular.y = ang_vel[1]
    twist_msg.twist.angular.z = ang_vel[2]
    return twist_msg
    

def make_event(x, y, ts, pol):
    e = Event()
    e.x = x
    e.y = y
    e.ts = rospy.Time(secs=ts)
    e.polarity = pol
    return e
  


if __name__ == '__main__':

    rospy.init_node('dvs_simulator_node', anonymous=True)
    rospack = rospkg.RosPack()

    # Load simulator parameters
    dataset_name = rospy.get_param('dataset_name', 'one_textured_plane_translation')
    cp = rospy.get_param('contrast_p', 0.15)
    cm = rospy.get_param('contrast_m', -cp)
    sigp = rospy.get_param('sigma_p', 0.0)
    sigm = rospy.get_param('sigma_m', 0.0)
    blur_size = rospy.get_param('blur_size', 0)
    
    event_streaming_rate = rospy.get_param('event_streaming_rate', 300)
    image_streaming_rate = rospy.get_param('image_streaming_rate', 24)
    write_to_bag = rospy.get_param('write_to_bag', False)
    check_optical_flow = rospy.get_param('check_optical_flow', False)
    
    rospy.loginfo('Dataset name: %s' % dataset_name)
    rospy.loginfo('Contrast threshold (+): %f' % cp)
    rospy.loginfo('Contrast threshold (-): %f' % cm)
    rospy.loginfo('Contrast threshold Std (+): %f' % sigp)
    rospy.loginfo('Contrast threshold Std (-): %f' % sigm)
    rospy.loginfo('Event streaming rate: %d packets / s' % event_streaming_rate)
    rospy.loginfo('Image streaming rate: %d images / s' % image_streaming_rate)
    
    delta_event = rospy.Duration(1.0 / event_streaming_rate)
    delta_image = rospy.Duration(1.0 / image_streaming_rate)
    
    # Parse dataset
    dataset_dir = os.path.join(rospack.get_path('rpg_datasets'), 'DVS', 'synthetic', 'full_datasets', dataset_name, 'data')
    times, img_paths, positions, orientations, cam = dataset_utils.parse_dataset(dataset_dir)   
    camera_info_msg = make_camera_msg(cam)
    
    _, v_body, w_body = dataset_utils.linear_angular_velocity(times, positions, orientations, stride=1)
    
    # Prepare publishers
    bridge = CvBridge()
    depthmap_pub = rospy.Publisher("/dvs/depthmap", Image, queue_size=0)
    image_pub = rospy.Publisher("/dvs/image_raw", Image, queue_size=0)
    pose_pub = rospy.Publisher("/dvs/pose", PoseStamped, queue_size=0)
    twist_pub = rospy.Publisher("/dvs/twist", TwistStamped, queue_size=0)
    camera_info_pub = rospy.Publisher("/dvs/camera_info", CameraInfo, queue_size=0)
    event_pub = rospy.Publisher("/dvs/events", EventArray, queue_size=0)
    
    if write_to_bag:
        bag_dir = os.path.join(rospack.get_path('rpg_datasets'), 'DVS', 'synthetic', 'rosbags', dataset_name)
        if not os.path.exists(bag_dir):
            os.makedirs(bag_dir)
        bag = rosbag.Bag('%s/%s-%s.bag' % (bag_dir, dataset_name, time.strftime("%Y%m%d-%H%M%S")), 'w')
    
    # Initialize DVS
    exr_img = OpenEXR.InputFile('%s/%s' % (dataset_dir, img_paths[0]))
    
    img = dataset_utils.extract_grayscale(exr_img)
    
    if blur_size > 0:
        img = cv2.GaussianBlur(img, (blur_size,blur_size), 0)
    
    init_sensor = dataset_utils.safe_log(img)
    init_time = rospy.Time(times[0])
    last_pub_img_timestamp = init_time
    last_pub_event_timestamp = init_time
    events = []
    
    # Init simulator
    sim = DvsSimulator(init_time.to_sec(), init_sensor, cp)
    
    # Publish initial pose, image and depthmap
    if write_to_bag:
        bag.write(topic='/dvs/pose', msg=make_pose_msg(positions[0], orientations[0], init_time), t=init_time)        
        
        bag.write(topic='/dvs/contrast_p', msg=Float32(cp), t=init_time)
        bag.write(topic='/dvs/contrast_m', msg=Float32(cm), t=init_time)
        bag.write(topic='/dvs/sigma2_p', msg=Float32(sigp), t=init_time)
        bag.write(topic='/dvs/sigma2_m', msg=Float32(sigm), t=init_time)
        bag.write(topic='/dvs/blur_size', msg=Int16(blur_size), t=init_time)
        
        img_msg = bridge.cv2_to_imgmsg(np.uint8(img * 255.0), 'mono8')
        img_msg.header.stamp = init_time
        try:
            image_pub.publish(img_msg)
        except CvBridgeError as e:
            print(e)
                
        bag.write(topic='/dvs/image_raw', msg=img_msg, t=init_time)
        
        z = dataset_utils.extract_depth(exr_img)
        depth_msg = bridge.cv2_to_imgmsg(z, '32FC1')
        depth_msg.header.stamp = init_time
        try:
            depthmap_pub.publish(depth_msg)
        except CvBridgeError as e:
            print(e)
            
        bag.write(topic='/dvs/depthmap', msg=depth_msg, t=init_time)
           
    if not write_to_bag:
        # Do not start publishing events if no one is listening
        rate = rospy.Rate(100)
        while event_pub.get_num_connections() == 0:
            rate.sleep()

    # Start simulation
    for frame_id in range(1, len(times)):
        
        if rospy.is_shutdown():
            break
        
        timestamp = rospy.Time(times[frame_id])
        
        rospy.loginfo('Processing frame at time: %f' % timestamp.to_sec())
            
        # publish pose
        if pose_pub.get_num_connections() > 0:
            pose_pub.publish(make_pose_msg(positions[frame_id], orientations[frame_id], timestamp))
        
        if write_to_bag:
            bag.write(topic='/dvs/pose', msg=make_pose_msg(positions[frame_id], orientations[frame_id], timestamp), t=timestamp)
            
            try:
                lin_vel, ang_vel = v_body[frame_id], w_body[frame_id]
                bag.write(topic='/dvs/twist', msg=make_twist_msg(lin_vel, ang_vel, timestamp), t=timestamp)
            except IndexError:
                pass
            
        # publish camera_info
        if camera_info_pub.get_num_connections() > 0:
            camera_info_pub.publish(camera_info_msg)
            
        if write_to_bag:
            bag.write(topic='/dvs/camera_info', msg=camera_info_msg, t=timestamp)
        
        exr_img = OpenEXR.InputFile('%s/%s' % (dataset_dir, img_paths[frame_id]))
        img = dataset_utils.extract_grayscale(exr_img)
        
        if blur_size > 0:
            img = cv2.GaussianBlur(img, (blur_size,blur_size), 0)
            
        
        if timestamp - last_pub_img_timestamp > delta_image or timestamp == init_time:
            # publish image_raw
            if write_to_bag or image_pub.get_num_connections > 0:
                img_msg = bridge.cv2_to_imgmsg(np.uint8(img * 255.0), 'mono8')
                img_msg.header.stamp = timestamp
                try:
                    image_pub.publish(img_msg)
                except CvBridgeError as e:
                    print(e)
                
                if write_to_bag:
                    bag.write(topic='/dvs/image_raw', msg=img_msg, t=timestamp)
                    
            # publish depth_map
            if write_to_bag or depthmap_pub.get_num_connections() > 0:
                z = dataset_utils.extract_depth(exr_img)
                depth_msg = bridge.cv2_to_imgmsg(z, '32FC1')
                depth_msg.header.stamp = timestamp
                try:
                    depthmap_pub.publish(depth_msg)
                except CvBridgeError as e:
                        print(e)
 
                if write_to_bag:
                    bag.write(topic='/dvs/depthmap', msg=depth_msg, t=timestamp)
                
            last_pub_img_timestamp = timestamp
        
        # compute events for this frame
        img = dataset_utils.safe_log(img)
        current_events = sim.update(timestamp.to_sec(), img)
        events += current_events
        
        # check if predicted optical flow is below the contrast threshold        
        if check_optical_flow:
            try:
                lin_vel, ang_vel = v_body[frame_id], w_body[frame_id]
            except IndexError:
                lin_vel, ang_vel = v_body[frame_id-1], w_body[frame_id-1]
                
            z = dataset_utils.extract_depth(exr_img)
            motion_field = mf.compute_motion_field(cam, z, lin_vel, ang_vel)
            gradient = dataset_utils.compute_gradient(img)
            gradient = np.reshape(gradient, (gradient.shape[0]*gradient.shape[1],2))
            motion_field = np.reshape(motion_field, (motion_field.shape[0]*motion_field.shape[1],2))
            dt = times[frame_id]-times[frame_id-1]
            deltaI = - (motion_field[:,0] * gradient[:,0] + motion_field[:,1] * gradient[:,1]) * dt
            assert(np.max(deltaI) <= cp)
            assert(np.min(deltaI) >= -cp)
        
        # publish events
        if timestamp - last_pub_event_timestamp > delta_event:
            events = sorted(events, key=lambda e: e.ts)
            event_array = EventArray()
            event_array.header.stamp = timestamp
            event_array.width = cam.width
            event_array.height = cam.height
            event_array.events = events
            event_pub.publish(event_array)
            
            if write_to_bag:
                bag.write(topic='/dvs/events', msg=event_array, t=timestamp)
            
            events = []
            last_pub_event_timestamp = timestamp
            
    if write_to_bag:       
        bag.close()
        rospy.loginfo('Finished writing rosbag')
    
