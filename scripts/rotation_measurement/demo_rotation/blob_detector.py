#!/usr/bin/env python
from asyncore import write
import numpy as np
import cv2
import pdb

import rospy, timeit
from time import sleep
from sensor_msgs.msg import Image
from std_msgs.msg import Empty as EmptyMsg
from cv_bridge import CvBridge, CvBridgeError
from std_srvs.srv import Empty, EmptyResponse
from grasp_executor.srv import AngleTrack
import matplotlib.pyplot as plt
from filterpy.kalman import KalmanFilter

from enum import Enum

import sys

PYTHON3 = sys.version_info.major == 3


class Quadrant(Enum):
    INIT = 0
    NW = 1
    SW = 2
    SE = 3
    NE = 4


class AngleDetectorService:
    def __init__(self):
        rospy.init_node("Angle_detector")
        # self.image_topic = "/realsense/rgb"

        self.AD = AngleDetector(writeImages=True, showImages=True, cv2Image=False)
        # self.current_image = None
        # rospy.Subscriber(self.image_topic, Image, self.image_callback)
        rospy.Service("track_angle", AngleTrack, self.update_angle)
        rospy.Service("reset_angle_tracking", Empty, self.reset_tracking)

        rospy.loginfo("Angle tracker ready!")
        rospy.spin()

    def update_angle(self, req):
        # print(dir(req))
        return self.AD.update_angle(req.im)

    def reset_tracking(self, req):
        self.AD.reset_tracking()
        return EmptyResponse()


class AngleDetector:
    def __init__(self, writeImages=True, showImages=True, cv2Image=False):

        self.bridge = CvBridge()
        self.state = Quadrant.INIT
        self.closest_state = Quadrant.INIT


        self.f = KalmanFilter (dim_x=2, dim_z=1)

        self.initial_state_set = False

        # transition matrix
        self.f.F = np.array([[1.,1/60],
            [0.,1.]])

        # measurement function
        self.f.H = np.array([[1.,0.]])

        # covairance function
        self.f.P *= 0.00001
        # low measurement noise
        self.f.R = 0.00001

        from filterpy.common import Q_discrete_white_noise
        self.f.Q = Q_discrete_white_noise(dim=2, dt=0.1, var=0.13)


        self.angle = None
        self.startAngle = None
        self.largeChange = False
        self.calculatedAngle = None
        self.angular_velocity = 0
        self.prev_angle = None
        self.calc_time = None
        self.writeImages = writeImages
        self.showImages = showImages
        self.cv2Image = cv2Image
        self.videoNumber = 0
        self.videoWriter = cv2.VideoWriter(
            str(self.videoNumber) + ".avi",
            cv2.VideoWriter_fourcc("M", "J", "P", "G"),
            5,
            (600, 300),
        )
        # self.videoWriter2 = cv2.VideoWriter("original" + str(self.videoNumber) + '.avi',cv2.VideoWriter_fourcc('M','J','P','G'), 60, (300,300))

    def reset_tracking(self):
        self.state = Quadrant.INIT
        self.closest_state = Quadrant.INIT
        self.angle = None
        self.calculatedAngle = None
        self.largeChange = False
        self.angular_velocity = 0
        self.calc_time = None
        self.videoNumber += 1
        self.videoWriter.release()
        # self.videoWriter2.release()

        self.videoWriter = cv2.VideoWriter(
            str(self.videoNumber) + ".avi",
            cv2.VideoWriter_fourcc("M", "J", "P", "G"),
            5,
            (600, 300),
        )
        # self.videoWriter2 = cv2.VideoWriter("original" + str(self.videoNumber) + '.avi',cv2.VideoWriter_fourcc('M','J','P','G'), 60, (300,300))

        print("reset!")

    def getAngle(self):
        return self.angle - self.startAngle

    def getAngularVelocity(self):
        # return self.angular_velocity
        return self.f.x[1]

    def angle_calculation(self, point0, point1):
        if point0[0] - point1[0] == 0:
            temp_angle = 90
        else:
            temp_angle = (
                np.arctan(
                    np.abs(float(point0[1] - point1[1]) / float(point0[0] - point1[0]))
                )
                * 180
                / np.pi
            )

        self.prev_angle = self.angle

        if self.state == Quadrant.NW:
            temp_angle = -temp_angle
        elif self.state == Quadrant.SW:
            temp_angle = temp_angle
        elif self.state == Quadrant.SE:
            temp_angle = 180 - temp_angle
        elif self.state == Quadrant.NE:
            temp_angle = -180 + temp_angle

        if (self.prev_angle is None):
            self.angle = temp_angle
            
        elif abs(temp_angle - self.prev_angle) > 20:
            # very large change
            self.largeChange = True
            pass
        else:
            self.angle = temp_angle

        if self.startAngle is None:
            self.startAngle = self.angle

        self.calculatedAngle = temp_angle
        current_time = timeit.default_timer()

        # if self.calc_time:
        #     # print(self.angle, prev_angle, self.calc_time, current_time)
        #     new_angular_velocity = (self.angle - self.prev_angle) / (
        #         current_time - self.calc_time
        #     )
        #     self.angular_velocity = (
        #         0.8 * new_angular_velocity + 0.2 * self.angular_velocity
        #     )
        z = self.angle
        if not self.initial_state_set:
            self.initial_state_set = True
            # initial state
            self.f.x = np.array([z, 0.])
        else:
            self.f.predict()
            self.f.update([z])
            try:
                self.angular_velocity = (z - self.prev_angle) / (current_time - self.calc_time)
            except:
                pdb.set_trace()

        self.calc_time = current_time

    def state_update(self, point0, point1):
        rightmost_point = 0 if point0[0] > point1[0] else 1
        lowest_point = 0 if point0[1] > point1[1] else 1

        if self.state == Quadrant.INIT:
            self.state = Quadrant.NW if rightmost_point == lowest_point else Quadrant.SW
            self.closest_state = (
                Quadrant.SW if rightmost_point == lowest_point else Quadrant.NW
            )
        elif self.state == Quadrant.NW:
            # print(rightmost_point, lowest_point, self.state, self.closest_state)
            if rightmost_point is not lowest_point:
                self.state = self.closest_state
        elif self.state == Quadrant.SW:
            if rightmost_point is lowest_point:
                self.state = self.closest_state
        elif self.state == Quadrant.SE:
            if rightmost_point is not lowest_point:
                self.state = self.closest_state
        elif self.state == Quadrant.NE:
            if rightmost_point is lowest_point:
                self.state = self.closest_state

    def closest_new_state(self):
        self.closest_state = Quadrant.INIT
        if self.state == Quadrant.NW:
            self.closest_state = Quadrant.SW if self.angle > -45 else Quadrant.NE
        elif self.state == Quadrant.SW:
            self.closest_state = Quadrant.NW if self.angle < 45 else Quadrant.SE
        elif self.state == Quadrant.SE:
            self.closest_state = Quadrant.SW if self.angle < 135 else Quadrant.NE
        elif self.state == Quadrant.NE:
            self.closest_state = Quadrant.NW if self.angle > -135 else Quadrant.SE

    def update_angle(self, im):
        # print("Reached loop!")
        # image = cv2.imread('img_temp.jpeg')
        if not self.cv2Image:
            im = self.bridge.imgmsg_to_cv2(im, desired_encoding="8UC3")

        result = im.copy()
        image = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)

        lower = np.array([0, 85, 0])
        upper = np.array([7, 255, 255])
        mask = cv2.inRange(image, lower, upper)

        lower1 = np.array([175, 85, 0])
        upper1 = np.array([180, 255, 255])
        mask1 = cv2.inRange(image, lower1, upper1)

        mask = mask + mask1

        result = cv2.bitwise_and(result, result, mask=mask)

        if self.showImages:
            cv2.imshow("orig", im)
            cv2.imshow("hsv", image)
            cv2.imshow("mask", mask)
            # cv2.waitKey(1)

        if self.writeImages:
            cv2.imwrite("img_temp.jpeg", im)
            cv2.imwrite("mask.jpeg", mask)
            cv2.imwrite("result.jpeg", result)

        contours = None
        if PYTHON3:
            # print(len(cv2.findContours(
            #     mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
            # )))

            contours, _ = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
            )
            contours = sorted(
                contours, key=lambda el: cv2.contourArea(el), reverse=True
            )

        else:
            _, contours, _ = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
            )
            contours.sort(key=lambda el: cv2.contourArea(el), reverse=True)

        canvas = result.copy()

        M = cv2.moments(contours[0])
        center1 = (int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"]))
        cv2.circle(canvas, center1, 2, (0, 255, 0), -1)

        M = cv2.moments(contours[1])
        center2 = (int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"]))
        cv2.circle(canvas, center2, 2, (0, 255, 0), -1)

        if self.angle is None:
            self.state_update(center1, center2)
            self.angle_calculation(center1, center2)
        else:
            self.closest_new_state()
            self.state_update(center1, center2)
            self.angle_calculation(center1, center2)
        # print(canvas.shape)
        # if self.showImages:
        # print("start")

        # cv2.imshow("canvas", canvas)
        # cv2.waitKey(0)
        # cv2.destroyAllWindows()
        # plt.imshow(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
        # plt.show()
        # print("help")

        vis = np.concatenate((canvas, im), axis=1)
        if self.angle is not None and self.prev_angle is not None:
            cv2.putText(
                vis,
                f"prev angle: {self.prev_angle:.2f}, current: {self.angle:.2f}, delta: {(self.angle - self.prev_angle):.2f}",
                (20, 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (0, 255, 0),
                1,
            )

        # print(vis.shape)
        self.videoWriter.write(vis)

        if self.writeImages:
            cv2.imwrite("canvas.jpeg", canvas)
        # print(self.angle)
        return self.calculatedAngle, self.largeChange


if __name__ == "__main__":
    try:
        angle_class = AngleDetectorService()
    except KeyboardInterrupt:
        pass
