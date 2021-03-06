#! /usr/bin/env python

import copy
import datetime
import itertools
import threading
import numpy as np
from shapely.geometry import Point, Polygon
from scipy.spatial.distance import euclidean

import tf
import rospy
import message_filters
from std_msgs.msg import Header
from geometry_msgs.msg import PoseStamped, PoseArray

from robblog.msg import RobblogEntry
from soma_map_manager.srv import MapInfo
from soma_manager.srv import SOMAQueryROIs
from vision_people_logging.msg import LoggingUBD
from vision_people_logging.srv import CaptureUBD
from bayes_people_tracker.msg import PeopleTracker
from mongodb_store.message_store import MessageStoreProxy


def create_polygon(xs, ys):
    # if poly_area(np.array(xs), np.array(ys)) == 0.0:
    if Polygon(np.array(zip(xs, ys))).area == 0.0:
        xs = [
            [xs[0]] + list(i) for i in itertools.permutations(xs[1:])
        ]
        ys = [
            [ys[0]] + list(i) for i in itertools.permutations(ys[1:])
        ]
        areas = list()
        for ind in range(len(xs)):
            # areas.append(poly_area(np.array(xs[ind]), np.array(ys[ind])))
            areas.append(Polygon(np.array(zip(xs[ind], ys[ind]))))
        return Polygon(
            np.array(zip(xs[areas.index(max(areas))], ys[areas.index(max(areas))]))
        )
    else:
        return Polygon(np.array(zip(xs, ys)))


def get_soma_info(soma_config):
    soma_service = rospy.ServiceProxy("/soma/map_info", MapInfo)
    soma_service.wait_for_service()
    soma_map = soma_service(1).map_name
    rospy.loginfo("Got soma map name %s..." % soma_map)
    # get region information from soma2
    soma_service = rospy.ServiceProxy("/soma/query_rois", SOMAQueryROIs)
    soma_service.wait_for_service()
    result = soma_service(
        query_type=0, roiconfigs=[soma_config], returnmostrecent=True
    )
    # create polygon for each regions
    regions = dict()
    for region in result.rois:
        if region.config == soma_config and region.map_name == soma_map:
            xs = [pose.position.x for pose in region.posearray.poses]
            ys = [pose.position.y for pose in region.posearray.poses]
            regions[region.id] = create_polygon(xs, ys)
    rospy.loginfo("Total regions for configuration %s are %d" % (soma_config, len(regions.values())))
    return regions, soma_map


class PeopleCounter(object):

    def __init__(self, config, region_categories=dict(), coll='activity_robblog'):
        rospy.loginfo("Starting activity checking...")
        self.region_categories = region_categories
        self._lock = False
        # regions = {roi:polygon} and soma map info
        self.regions, self.soma_map = get_soma_info(config)
        self.reset()
        # tf stuff
        self._tfl = tf.TransformListener()
        # create db
        rospy.loginfo("Create database collection %s..." % coll)
        self._db = MessageStoreProxy(collection=coll)
        self._db_image = MessageStoreProxy(collection=coll+"_img")
        self._ubd_db = MessageStoreProxy(collection="upper_bodies")
        # service client to upper body logging
        rospy.loginfo("Create client to /vision_logging_service/capture...")
        self.capture_srv = rospy.ServiceProxy(
            "/vision_logging_service/capture", CaptureUBD
        )
        # subscribing to ubd topic
        subs = [
            message_filters.Subscriber(
                rospy.get_param(
                    "~ubd_topic", "/upper_body_detector/bounding_box_centres"
                ),
                PoseArray
            ),
            message_filters.Subscriber(
                rospy.get_param("~tracker_topic", "/people_tracker/positions"),
                PeopleTracker
            )
        ]
        ts = message_filters.ApproximateTimeSynchronizer(
            subs, queue_size=5, slop=0.15
        )
        ts.registerCallback(self.cb)

    def reset(self):
        # start modified code
        self.detected_time = dict()
        # end modified code
        self.uuids = {roi: list() for roi, _ in self.regions.iteritems()}
        self.image_ids = {roi: list() for roi, _ in self.regions.iteritems()}
        self.people_poses = list()
        self._stop = False
        self._ubd_pos = list()
        self._tracker_pos = list()
        self._tracker_uuids = list()

    def cb(self, ubd_cent, pt):
        if not self._lock:
            self._lock = True
            self._tracker_uuids = pt.uuids
            self._ubd_pos = self.to_world_all(ubd_cent)
            self._tracker_pos = [i for i in pt.poses]
            self._lock = False

    def to_world_all(self, pose_arr):
        transformed_pose_arr = list()
        try:
            fid = pose_arr.header.frame_id
            for cpose in pose_arr.poses:
                ctime = self._tfl.getLatestCommonTime(fid, "/map")
                pose_stamped = PoseStamped(Header(1, ctime, fid), cpose)
                # Get the translation for this camera's frame to the world.
                # And apply it to all current detections.
                tpose = self._tfl.transformPose("/map", pose_stamped)
                transformed_pose_arr.append(tpose.pose)
        except tf.Exception as e:
            rospy.logwarn(e)
            # In case of a problem, just give empty world coordinates.
            return []
        return transformed_pose_arr

    def stop_check(self):
        self._stop = True

    def _is_new_person(self, ubd_pose, track_pose, tracker_ind):
        pose_inside_roi = ''
        # merge ubd with tracker pose
        cond = euclidean(
            [ubd_pose.position.x, ubd_pose.position.y],
            [track_pose.position.x, track_pose.position.y]
        ) < 0.2
        # uuid must be new
        if cond:
            is_new = True
            for roi, uuids in self.uuids.iteritems():
                if self._tracker_uuids[tracker_ind] in uuids:
                    is_new = False
                    break
            cond = cond and is_new
            if cond:
                # this pose must be inside a region
                for roi, region in self.regions.iteritems():
                    if region.contains(
                        Point(ubd_pose.position.x, ubd_pose.position.y)
                    ):
                        pose_inside_roi = roi
                        break
                cond = cond and (pose_inside_roi != '')
                if cond:
                    is_near = False
                    for pose in self.people_poses:
                        if euclidean(
                            pose, [ubd_pose.position.x, ubd_pose.position.y]
                        ) < 0.3:
                            is_near = True
                            break
                    cond = cond and (not is_near)
        return cond, pose_inside_roi

    def _uuids_roi_to_category(self, dict_uuids):
        result = dict()
        for roi, uuids in dict_uuids.iteritems():
            region_category = roi
            if region_category in self.region_categories:
                region_category = self.region_categories[region_category]
            if region_category not in result:
                result[region_category] = (list(), list())
            result[region_category][0].append(roi)
            result[region_category][1].extend(uuids)
        return result

    def _create_robmsg(self, start_time, end_time):
        regions_to_string = dict()
        regions_to_string_img = dict()
        for region_category, (rois, uuids) in self._uuids_roi_to_category(self.uuids).iteritems():
            if region_category not in regions_to_string:
                regions_to_string[region_category] = '# Activity Report \n'
                regions_to_string[region_category] += ' * **Regions:** %s \n' % str(rois)
                regions_to_string[region_category] += ' * **Area:** %s \n' % region_category
                regions_to_string[region_category] += ' * **Start time:** %s \n' % str(start_time)
                regions_to_string[region_category] += ' * **End time:** %s \n' % str(end_time)
                regions_to_string[region_category] += ' * **Summary:** %d person(s) were detected \n' % len(uuids)
                regions_to_string[region_category] += ' * **Details:** \n\n'
                regions_to_string_img[region_category] = copy.copy(regions_to_string[region_category])
            for roi in rois:
                for ind, uuid in enumerate(self.uuids[roi]):
                    try:
                        detected_time = self.detected_time[uuid]
                    except:
                        detected_time = start_time
                    detected_time = datetime.datetime.fromtimestamp(detected_time.secs)
                    regions_to_string[region_category] += '%s was detected at %s \n\n' % (uuid, detected_time)
                    regions_to_string_img[region_category] += "![%s](ObjectID(%s)) " % (
                        uuid, self.image_ids[roi][ind]
                    )
                    regions_to_string_img[region_category] += 'was detected at %s \n\n' % str(detected_time)

        entries = list()
        for region_category, string_body in regions_to_string.iteritems():
            entries.append(RobblogEntry(
                title="%s Activity Report - %s" % (start_time.date(), region_category),
                body=string_body
            ))

        entry_images = list()
        for region_category, string_body in regions_to_string_img.iteritems():
            entry_images.append(RobblogEntry(
                title="%s Activity Report - %s" % (start_time.date(), region_category),
                body=string_body
            ))
        return entries, entry_images

    def _store(self, start_time, end_time):
        rospy.loginfo("Storing location and the number of detected persons...")
        start_time = datetime.datetime.fromtimestamp(start_time.secs)
        end_time = datetime.datetime.fromtimestamp(end_time.secs)
        entries, entry_images = self._create_robmsg(start_time, end_time)
        for entry in entries:
            self._db.insert(entry)
        for entry in entry_images:
            self._db_image.insert(entry)

    def continuous_check(self, duration):
        rospy.loginfo("Start looking for people...")
        start_time = rospy.Time.now()
        end_time = rospy.Time.now()
        while (end_time - start_time) < duration and not self._stop:
            if not self._lock:
                self._lock = True
                for ind_ubd, i in enumerate(self._ubd_pos):
                    for ind, j in enumerate(self._tracker_pos):
                        cond, pose_inside_roi = self._is_new_person(i, j, ind)
                        if cond:
                            result = self.capture_srv()
                            rospy.sleep(0.1)
                            _id = ""
                            if len(result.obj_ids) > 0:
                                ubd_log = self._ubd_db.query_id(
                                    result.obj_ids[0], LoggingUBD._type
                                )
                                try:
                                    _id = self._db_image.insert(
                                        ubd_log[0].ubd_rgb[ind_ubd]
                                    )
                                except:
                                    rospy.logwarn(
                                        "Missed the person to capture images..."
                                    )
                            self.image_ids[pose_inside_roi].append(_id)
                            # self.uuids.append(self._tracker_uuids[ind])
                            self.uuids[pose_inside_roi].append(
                                self._tracker_uuids[ind]
                            )
                            if self._tracker_uuids[ind] not in self.detected_time:
                                self.detected_time[self._tracker_uuids[ind]] = end_time
                            self.people_poses.append(
                                [i.position.x, i.position.y]
                            )
                            rospy.loginfo(
                                "%s is detected in region %s - (%.2f, %.2f)" % (
                                    self._tracker_uuids[ind], pose_inside_roi,
                                    i.position.x, i.position.y
                                )
                            )
                self._lock = False
            end_time = rospy.Time.now()
            rospy.sleep(0.1)
        self._store(start_time, end_time)
        self._stop = False


if __name__ == '__main__':
    rospy.init_node("activity_checking")
    soma_config = rospy.get_param("~soma_config", "activity_exploration")
    ac = PeopleCounter(soma_config)
    thread = threading.Thread(
        target=ac.continuous_check, args=(rospy.Duration(60),)
    )
    thread.start()
    rospy.sleep(10)
    ac.stop_check()
    thread.join()
