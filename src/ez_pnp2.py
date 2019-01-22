#!/usr/bin/env python
import tf
import sys
import time
import rospy
import random
import moveit_commander

from grasp_planning_graspit_msgs.srv import AddToDatabase, LoadDatabaseModel, AddToDatabaseRequest, LoadDatabaseModelRequest
from ez_pick_and_place.srv import EzSceneSetup, EzSceneSetupResponse, EzStartPlanning
from household_objects_database_msgs.msg import DatabaseModelPose
from geometry_msgs.msg import TransformStamped, PoseStamped, Pose
from manipulation_msgs.msg import GraspableObject
from manipulation_msgs.srv import GraspPlanning
from std_srvs.srv import Trigger

import ezpnp_sim_annealing

tf_listener = None
moveit_scene = None
planning_srv = None
add_model_srv = None
load_model_srv = None

ez_objects = dict()
ez_obstacles = dict()

gripper_name = None
gripper_frame = None

keep_planning = True

def stopPlanning(req):
    keep_planning = False
    return True, ""

def reset(arm_move_group, req):
    if req.reset_position:
        arm_move_group.set_named_target(req.reset_position)
        return arm_move_group.go()

def move(arm_move_group, pose):
    arm_move_group.set_pose_target(pose)
    return arm_move_group.go()

def graspThis(object_name):
    global ez_objects, gripper_name

    dbmp = DatabaseModelPose()
    dbmp.model_id = ez_objects[object_name][0]
    dbmp.confidence = 1
    dbmp.detector_name = "manual_detection"
    planning_req = GraspPlanning()
    target = GraspableObject()
    target.reference_frame_id = "1"
    target.potential_models = [dbmp]
    response = planning_srv(arm_name = gripper_name, target = target)

    return response.grasps

def attachThis(object_name, arm_move_group, gripper_move_group_name):
    global moveit_scene

    robot_commander = moveit_commander.RobotCommander()
    touch_links = robot_commander.get_link_names(gripper_move_group_name)

    moveit_scene.attach_mesh(arm_move_group.get_end_effector_link(), name=object_name, pose=None, touch_links=touch_links)

def nextGraspIndex(next_grasp_index, grasps):
    next_grasp_index += 1
    looped = False
    if next_grasp_index >= len(grasps):
        next_grasp_index = 0
        looped = True
    return next_grasp_index, looped

# TODO use set_start_state of the move group
# so that we can plan the whole thing without 
# moving the robot!
# TODO2 send a moveit goal for the gripper positions,
# and since SCHUNK PG70 drivers suck,
# create a wrapper of those messages to make the required
# service calls to the PG70 drivers
def startPlanning(req):
    global keep_planning, gripper_frame, moveit_scene

    robot_commander = moveit_commander.RobotCommander()

    arm_move_group = moveit_commander.MoveGroupCommander(req.arm_move_group)

    keep_planning = True
    remaining_secs = req.secs_to_timeout
    timeout_disabled = req.secs_to_timeout <= 0
    t0 = time.clock()
    on_reset_pose = False
    away_from_grasp_pose = True
    # TODO add info on service regarding a reset position
    try:
        holding_object = False
        graspit_grasps = graspThis(req.graspit_target_object)
        fixed_grasps = translateGraspIt2MoveIt(graspit_grasps, arm_move_group.get_end_effector_link(), req.graspit_target_object)
        next_grasp_index = 0
        near_grasp_pose = PoseStamped()
        near_place_pose = PoseStamped()
        while keep_planning and (timeout_disabled or remaining_secs > 0) and not rospy.is_shutdown():
            if not timeout_disabled:
                remaining_secs -= time.clock() - t0
            try:
                if not holding_object:
                    near_grasp_pose = calcNearGraspPose(fixed_grasps[next_grasp_index])
                    # Did we successfully move to the pre-grasping position?
                    if move(arm_move_group, near_grasp_pose):
                        on_reset_pose = False
                        print "Reached pregrasp pose!"
                        time.sleep(2)
                        if move(arm_move_group, fixed_grasps[next_grasp_index]):
                            away_from_grasp_pose = False
                            print "Reached grasp pose!"
                            time.sleep(2)
                            # TODO send grasp command
                            print "Holding the object!"
                            attachThis(req.graspit_target_object, arm_move_group, req.gripper_move_group)
                            time.sleep(5)
                            holding_object = True
                            continue
                        else:
                            next_grasp_index, looped = nextGraspIndex(next_grasp_index, fixed_grasps)
                            if looped and req.allow_replanning:
                                graspit_grasps = graspThis(req.graspit_target_object)
                                fixed_grasps = translateGraspIt2MoveIt(graspit_grasps, arm_move_group.get_end_effector_link(), req.graspit_target_object)
                                next_grasp_index = 0
                            continue
                    else:
                        if not on_reset_pose and reset(arm_move_group, req):
                            on_reset_pose = True
                        next_grasp_index, looped = nextGraspIndex(next_grasp_index, fixed_grasps)
                        if looped and req.allow_replanning:
                            graspit_grasps = graspThis(req.graspit_target_object)
                            fixed_grasps = translateGraspIt2MoveIt(graspit_grasps, arm_move_group.get_end_effector_link(), req.graspit_target_object)
                            next_grasp_index = 0
                else:
                    target_pose = calcTargetPose(req.target_place, near_grasp_pose)
                    near_place_pose = calcNearPlacePose(target_pose)
                    if not away_from_grasp_pose and move(arm_move_group, near_grasp_pose):
                        on_reset_pose = False
                        away_from_grasp_pose = True
                        print "Reached postgrasp pose!"
                        time.sleep(2)
                        continue
                    elif away_from_grasp_pose and move(arm_move_group, near_place_pose):
                        print "Reached preplace pose!"
                        time.sleep(2)
                        if move(arm_move_group, target_pose):
                            print "Reached place pose!"
                            time.sleep(2)
                            # TODO send ungrip command
                            print "Placed the object!"
                            moveit_scene.remove_attached_object(arm_move_group.get_end_effector_link(), req.graspit_target_object)
                            time.sleep(5)
                            holding_object = False
                            # stop trying now, but also try as a last move to
                            # reach the preplace pose again
                            move(arm_move_group, near_place_pose)
                            return True, "That was smoooooth :)"
                    elif not on_reset_pose and reset(arm_move_group, req):
                            on_reset_pose = True
                            away_from_grasp_pose = True

            except Exception as e:
                print str(e)
    except Exception as e:
        print str(e)
        return False, str(e)
    if not timeout_disabled and remaining_secs <= 0:
        return False, "Timeout!"
    return True, ""

# Check if the input of the scene setup service is valid
def validSceneSetupInput(req):
    tmp = dict()
    tmp2 = EzSceneSetupResponse()
    info = []
    error_codes = []
    if len(req.finger_joint_names) == 0:
        info.append("Invalid service input: No finger_joint_names provided")
        error_codes.append(tmp2.NO_FINGER_JOINTS)
        return False, info, error_codes
    if req.gripper.name == "":
        info.append("Invalid service input: No gripper name provided")
        error_codes.append(tmp2.NO_NAME)
        return False, info, error_codes
    if req.gripper.graspit_file == "":
        info.append("Invalid service input: No graspit filename provided for the gripper")
        error_codes.append(tmp2.NO_FILENAME)
        return False, info, error_codes
    if req.pose_factor <= 0:
        info.append("Invalid service input: pose_factor cannot be negative or zero")
        error_codes.append(tmp2.INVALID_POSE_FACTOR)
        return False, info, error_codes

    for obj in req.objects:
        if obj.name == "":
            info.append("Invalid service input: No object name provided")
            error_codes.append(tmp2.NO_NAME)
            return False, info, error_codes
        if obj.name in tmp:
            info.append("Invalid service input: Duplicate name: " + obj.name)
            error_codes.append(tmp2.DUPLICATE_NAME)
            return False, info, error_codes
        else:
            tmp[obj.name] = 0
        if obj.graspit_file == "" and obj.moveit_file == "":
            info.append("Invalid service input: No file provided for object: " + obj.name)
            error_codes.append(tmp2.NO_FILENAME)
            return False, info, error_codes
        if obj.pose.header.frame_id == "":
            info.append("Invalid service input: No frame_id in PoseStamped message of object: " + obj.name)
            error_codes.append(tmp2.NO_FRAME_ID)
            return False, info, error_codes

    for obs in req.obstacles:
        if obs.name == "":
            info.append("Invalid service input: No obstacle name provided")
            error_codes.append(tmp2.NO_NAME)
            return False, info, error_codes
        if obs.name in tmp:
            info.append("Invalid service input: Duplicate name: " + obs.name)
            error_codes.append(tmp2.DUPLICATE_NAME)
            return False, info, error_codes
        else:
            tmp[obs.name] = 0
        if obs.graspit_file == "" and obs.moveit_file == "":
            info.append("Invalid service input: No file provided for obstacle: " + obs.name)
            error_codes.append(tmp2.NO_FILENAME)
            return False, info, error_codes
        if obs.pose.header.frame_id == "":
            info.append("Invalid service input: No frame_id in PoseStamped message of obstacle: " + obs.name)
            error_codes.append(tmp2.NO_FRAME_ID)
            return False, info, error_codes
    return True, info, error_codes

# Graspit bodies are always referenced relatively to the "world" frame
def fixItForGraspIt(obj, pose_factor):
    global tf_listener
    p = Pose()
    if obj.pose.header.frame_id == "world":
        p.position.x = obj.pose.pose.position.x * pose_factor
        p.position.y = obj.pose.pose.position.y * pose_factor
        p.position.z = obj.pose.pose.position.z * pose_factor
        p.orientation.x = obj.pose.pose.orientation.x
        p.orientation.y = obj.pose.pose.orientation.y
        p.orientation.z = obj.pose.pose.orientation.z
        p.orientation.w = obj.pose.pose.orientation.w
        #TODO orientation?
    else:
        try:
            transform = TransformStamped()
            transform.header.stamp = rospy.Time.now()
            transform.header.frame_id = obj.pose.header.frame_id
            transform.child_frame_id = "ez_fix_it_for_grasp_it"
            transform.transform.translation.x = obj.pose.pose.position.x
            transform.transform.translation.y = obj.pose.pose.position.y
            transform.transform.translation.z = obj.pose.pose.position.z
            transform.transform.rotation.x = obj.pose.pose.orientation.x
            transform.transform.rotation.y = obj.pose.pose.orientation.y
            transform.transform.rotation.z = obj.pose.pose.orientation.z
            transform.transform.rotation.w = obj.pose.pose.orientation.w
            tf_listener.setTransform(transform, "fixItForGraspIt")

            trans, rot = tf_listener.lookupTransform("ez_fix_it_for_grasp_it", "world", rospy.Time(0))

            p.position.x = trans[0] * pose_factor
            p.position.y = trans[1] * pose_factor
            p.position.z = trans[2] * pose_factor
            p.orientation.x = rot[0]
            p.orientation.y = rot[1]
            p.orientation.z = rot[2]
            p.orientation.w = rot[3]
        except Exception as e:
            print e

    return p

# GraspIt and MoveIt appear to have a 90 degree difference in the x axis (roll 90 degrees)
def translateGraspIt2MoveIt(grasps, eef_link, object_name):
    global ez_objects, gripper_frame
    fixed_grasps = []
    for g in grasps:
        try:
            # World -> Object
            transform = TransformStamped()
            transform.header.stamp = rospy.Time.now()
            transform.header.frame_id = "world"
            transform.child_frame_id = "target_object_frame"
            transform.transform.translation.x = ez_objects[object_name][1].pose.position.x
            transform.transform.translation.y = ez_objects[object_name][1].pose.position.y
            transform.transform.translation.z = ez_objects[object_name][1].pose.position.z
            transform.transform.rotation.x = ez_objects[object_name][1].pose.orientation.x
            transform.transform.rotation.y = ez_objects[object_name][1].pose.orientation.y
            transform.transform.rotation.z = ez_objects[object_name][1].pose.orientation.z
            transform.transform.rotation.w = ez_objects[object_name][1].pose.orientation.w
            tf_listener.setTransform(transform, "ez_helper")

            # Object -> Gripper
            transform = TransformStamped()
            transform.header.stamp = rospy.Time.now()
            transform.header.frame_id = "target_object_frame"
            transform.child_frame_id = "ez_helper_graspit_pose"
            transform.transform.translation.x = g.grasp_pose.pose.position.x
            transform.transform.translation.y = g.grasp_pose.pose.position.y
            transform.transform.translation.z = g.grasp_pose.pose.position.z
            transform.transform.rotation.x = g.grasp_pose.pose.orientation.x
            transform.transform.rotation.y = g.grasp_pose.pose.orientation.y
            transform.transform.rotation.z = g.grasp_pose.pose.orientation.z
            transform.transform.rotation.w = g.grasp_pose.pose.orientation.w
            tf_listener.setTransform(transform, "ez_helper")

            transform_frame_gripper_trans, transform_frame_gripper_rot = tf_listener.lookupTransform(eef_link, gripper_frame, rospy.Time(0))

            # Gripper -> End Effector
            transform = TransformStamped()
            transform.header.stamp = rospy.Time.now()
            transform.header.frame_id = "ez_helper_graspit_pose"
            transform.child_frame_id = "ez_helper_fixed_graspit_pose"
            transform.transform.translation.x = -transform_frame_gripper_trans[0]
            transform.transform.translation.y = -transform_frame_gripper_trans[1]
            transform.transform.translation.z = -transform_frame_gripper_trans[2]
            transform.transform.rotation.x = transform_frame_gripper_rot[0]
            transform.transform.rotation.y = transform_frame_gripper_rot[1]
            transform.transform.rotation.z = transform_frame_gripper_rot[2]
            transform.transform.rotation.w = transform_frame_gripper_rot[3]
            tf_listener.setTransform(transform, "ez_helper")

            # Graspit to MoveIt translation
            # (Gripper -> Gripper)
            graspit_moveit_transform = TransformStamped()
            graspit_moveit_transform.header.stamp = rospy.Time.now()
            graspit_moveit_transform.header.frame_id = "ez_helper_fixed_graspit_pose"
            graspit_moveit_transform.child_frame_id = "ez_helper_target_graspit_pose"
            graspit_moveit_transform.transform.rotation.x = 0.7071
            graspit_moveit_transform.transform.rotation.y = 0.0
            graspit_moveit_transform.transform.rotation.z = 0.0
            graspit_moveit_transform.transform.rotation.w = 0.7071
            tf_listener.setTransform(graspit_moveit_transform, "ez_helper")

            target_trans, target_rot = tf_listener.lookupTransform("world", "ez_helper_target_graspit_pose", rospy.Time(0))

            g.grasp_pose.header.frame_id = "world"
            g.grasp_pose.pose.position.x = target_trans[0]
            g.grasp_pose.pose.position.y = target_trans[1]
            g.grasp_pose.pose.position.z = target_trans[2]
            g.grasp_pose.pose.orientation.x = target_rot[0]
            g.grasp_pose.pose.orientation.y = target_rot[1]
            g.grasp_pose.pose.orientation.z = target_rot[2]
            g.grasp_pose.pose.orientation.w = target_rot[3]
            fixed_grasps.append(g.grasp_pose)
        except Exception as e:
            print e
    return fixed_grasps

def calcNearGraspPose(pose):
    # TODO fix the near strategy
    near_pose = PoseStamped()
    near_pose.header = pose.header
    near_pose.pose.position.x = pose.pose.position.x + random.uniform(-0.05, 0.05)
    near_pose.pose.position.y = pose.pose.position.y + random.uniform(-0.05, 0.05)
    near_pose.pose.position.z = pose.pose.position.z + random.uniform(-0.05, 0.15)
    near_pose.pose.orientation = pose.pose.orientation
    return near_pose

def calcNearPlacePose(target_pose):
    # TODO fix the near strategy
    near_pose = PoseStamped()
    near_pose.header = target_pose.header
    near_pose.pose.position.x = target_pose.pose.position.x + random.uniform(-0.05, 0.05)
    near_pose.pose.position.y = target_pose.pose.position.y + random.uniform(-0.05, 0.05)
    near_pose.pose.position.z = target_pose.pose.position.z + random.uniform(-0.05, 0.15)
    near_pose.pose.orientation = target_pose.pose.orientation
    return near_pose

def calcTargetPose(pose, grasp_pose):
    # TODO fix the situation of an exception
    # Currently, we are doomed
    global tf_listener
    target_pose = PoseStamped()
    if pose.header.frame_id != "world":
        try:
            transform = TransformStamped()
            transform.header.stamp = rospy.Time.now()
            transform.header.frame_id = pose.header.frame_id
            transform.child_frame_id = "ez_target_pose_calculator"
            transform.transform.translation.x = pose.pose.position.x
            transform.transform.translation.y = pose.pose.position.y
            transform.transform.translation.z = pose.pose.position.z
            transform.transform.rotation.x = pose.pose.orientation.x
            transform.transform.rotation.y = pose.pose.orientation.y
            transform.transform.rotation.z = pose.pose.orientation.z
            transform.transform.rotation.w = pose.pose.orientation.w
            tf_listener.setTransform(transform, "calcTargetPose")

            trans, rot = tf_listener.lookupTransform("world", "ez_target_pose_calculator", rospy.Time(0))
            target_pose.header.stamp = rospy.Time.now()
            target_pose.header.frame_id = "world"
            target_pose.pose.position.x = trans[0]
            target_pose.pose.position.y = trans[1]
            target_pose.pose.position.z = trans[2]
        except Exception as e:
            print e
    else:
        target_pose.header = pose.header
        target_pose.pose.position.x = pose.pose.position.x
        target_pose.pose.position.y = pose.pose.position.y
        target_pose.pose.position.z = pose.pose.position.z

    target_pose.pose.orientation = grasp_pose.pose.orientation
    target_pose.pose.position.z = grasp_pose.pose.position.z + 0.01
    return target_pose

def scene_setup(req):
    global add_model_srv, load_model_srv, planning_srv, tf_listener, moveit_scene
    global ez_objects, ez_obstacles, gripper_name, gripper_frame

    valid, info, ec = validSceneSetupInput(req)

    gripper_frame = req.gripper_frame

    if not valid:
        return valid, info, ec

    res = EzSceneSetupResponse()
    res.success = True

    try:
        for obj in req.objects:
            # ------ Graspit world ------
            if obj.graspit_file != "":
                atd = AddToDatabaseRequest()
                atd.filename = obj.graspit_file
                atd.isRobot = False
                atd.asGraspable = True
                atd.modelName = obj.name
                response = add_model_srv(atd)
                if response.returnCode != response.SUCCESS:
                    res.success = False
                    res.info.append("Error adding object " + obj.name + " to graspit database")
                    res.error_codes.append(response.returnCode)
                else:
                    objectID = response.modelID

                    loadm = LoadDatabaseModelRequest()
                    loadm.model_id = objectID
                    loadm.model_pose = fixItForGraspIt(obj, req.pose_factor)
                    response = load_model_srv(loadm)

                    ez_objects[obj.name] = [objectID, obj.pose]

                    if response.result != response.LOAD_SUCCESS:
                        res.success = False
                        res.info.append("Error loading object " + obj.name + " to graspit world")
                        res.error_codes.append(response.result)
            # ---------------------------

            # ------ Moveit scene -------
            if obj.moveit_file != "":
                moveit_scene.add_mesh(obj.name, obj.pose, obj.moveit_file)
            # ---------------------------
        for obstacle in req.obstacles:
            # ------ Graspit world ------
            if obstacle.graspit_file != "":
                atd = AddToDatabaseRequest()
                atd.filename = obstacle.graspit_file
                atd.isRobot = False
                atd.asGraspable = False
                atd.modelName = obstacle.name
                response = add_model_srv(atd)
                if response.returnCode != response.SUCCESS:
                    res.success = False
                    res.info.append("Error adding obstacle " + obstacle.name + " to graspit database")
                    res.error_codes.append(response.returnCode)
                else:
                    obstacleID = response.modelID

                    loadm = LoadDatabaseModelRequest()
                    loadm.model_id = obstacleID
                    loadm.model_pose = fixItForGraspIt(obstacle, req.pose_factor)
                    response = load_model_srv(loadm)

                    ez_obstacles[obstacle.name] = [obstacleID, obstacle.pose]

                    if response.result != response.LOAD_SUCCESS:
                        res.success = False
                        res.info.append("Error loading obstacle " + obstacle.name + " to graspit world")
                        res.error_codes.append(response.result)
            # ---------------------------

            # ------ Moveit scene -------
            if obstacle.moveit_file != "":
                moveit_scene.add_mesh(obstacle.name, obstacle.pose, obstacle.moveit_file)
            # ---------------------------

        # ------ Graspit world ------
        atd = AddToDatabaseRequest()
        atd.filename = req.gripper.graspit_file
        atd.isRobot = True
        atd.asGraspable = False
        atd.modelName = req.gripper.name
        atd.jointNames = req.finger_joint_names
        response = add_model_srv(atd)
        if response.returnCode != response.SUCCESS:
                res.success = False
                res.info.append("Error adding robot " + req.gripper.name + " to graspit database")
                res.error_codes.append(response.returnCode)
        else:
            gripper_name = req.gripper.name
            robotID = response.modelID

            loadm = LoadDatabaseModelRequest()
            loadm.model_id = robotID
            p = Pose()
            gripper_pos, gripper_rot = tf_listener.lookupTransform(gripper_frame, "world", rospy.Time(0))
            p.position.x = gripper_pos[0] * req.pose_factor
            p.position.y = gripper_pos[1] * req.pose_factor
            p.position.z = gripper_pos[2] * req.pose_factor
            # TODO orientation is not important (right?)
            loadm.model_pose = p
            response = load_model_srv(loadm)

            if response.result != response.LOAD_SUCCESS:
                res.success = False
                res.info.append("Error loading robot " + req.gripper.name + " to graspit world")
                res.error_codes.append(response.result)
        # ---------------------------

        return res

    except Exception as e:
        info.append(str(e))
        ec.append(res.EXCEPTION)
        return False, info, ec

def main():
    global add_model_srv, load_model_srv, planning_srv, tf_listener, moveit_scene

    moveit_commander.roscpp_initialize(sys.argv)
    rospy.init_node("ez_pnp")

    tf_listener = tf.TransformListener()
    moveit_scene = moveit_commander.PlanningSceneInterface()

    add_model_srv = rospy.ServiceProxy("/graspit_add_to_database", AddToDatabase)
    rospy.wait_for_service("/graspit_add_to_database")
    load_model_srv = rospy.ServiceProxy("/graspit_load_model", LoadDatabaseModel)
    rospy.wait_for_service("/graspit_load_model")
    planning_srv = rospy.ServiceProxy("/graspit_eg_planning", GraspPlanning)
    rospy.wait_for_service("/graspit_eg_planning")

    scene_srv = rospy.Service("ez_pnp/scene_setup", EzSceneSetup, scene_setup)
    start_srv = rospy.Service("ez_pnp/start_planning", EzStartPlanning, startPlanning)
    stop_srv = rospy.Service("ez_pnp/stop_planning", Trigger, stopPlanning)

    rospy.spin()

main()
