#include "gps_agent_pkg/superchickplugin.h"
#include "gps_agent_pkg/positioncontroller.h"
#include "gps_agent_pkg/trialcontroller.h"
#include "gps_agent_pkg/util.h"

namespace gps_control {

// Plugin constructor.
GPSSuperchickPlugin::GPSSuperchickPlugin()
{
    // Some basic variable initialization.
    controller_counter_ = 0;
    controller_step_length_ = 50;
}

// Destructor.
GPSSuperchickPlugin::~GPSSuperchickPlugin()
{
    // Nothing to do here, since all instance variables are destructed automatically.
}

// Initialize the object and store the robot state.
bool GPSSuperchickPlugin::init(ros::NodeHandle& n)
{
    ros::AsyncSpinner spinner(1);
    spinner.start();

    // Variables.
    std::string base_group, head_name, right_name;

/*    // construct a
    // :moveit_core:`RobotState` that maintains the configuration
    // of the robot.
    robot_state::RobotStatePtr RobotState(new robot_state::RobotState(RobotModel));
    RobotState->setToDefaultValues();

    

    sleep(3.0);
    
    moveit::planning_interface::MoveGroup *group;
    group = new moveit::planning_interface::MoveGroup("base_bladder");
    group->setEndEffectorLink("headnball_link");

    // We will use the :planning_scene_interface:`PlanningSceneInterface`
    // class to deal directly with the world.
    moveit::planning_interface::PlanningSceneInterface planning_scene_interface;  

    // Create a publisher for visualizing plans in Rviz.
    ros::Publisher display_publisher = node_handle.advertise<moveit_msgs::DisplayTrajectory>("/move_group/display_planned_path", 1, true);
    moveit_msgs::DisplayTrajectory display_trajectory;

    // We can print the name of the reference frame for this robot.
    ROS_INFO("Reference frame: %s", group->getPlanningFrame().c_str());
    // print the name of the end-effector link for this group.
    robot_state::RobotState begin_state(*group->getCurrentState());

    const robot_state::JointModelGroup *base_model_group =
                    begin_state.getJointModelGroup(group->getName());

    std::string end_effector_id = base_model_group->getLinkModelNames().back();
    ROS_INFO_STREAM("Reference frame: " << end_effector_id);*/


    // Create FK solvers.
    // Get the name of the root.
    if(!n.getParam("/GPSSuperchickPlugin/base_group", base_group)) {
        ROS_ERROR("Property base_group not found in namespace: '%s'", n.getNamespace().c_str());
        return false;
    }

    // Get active and passive arm end-effector names.
    if(!n.getParam("/GPSSuperchickPlugin/head_name", head_name)) {
        ROS_ERROR("Property head_name not found in namespace: '%s'", n.getNamespace().c_str());
        return false;
    }
    if(!n.getParam("/GPSSuperchickPlugin/right_name", right_name)) {
        ROS_ERROR("Property right_name not found in namespace: '%s'", n.getNamespace().c_str());
        return false;
    }

    RobotModel = robot_model_loader->getModel();    
    ROS_INFO("Model frame: %s", RobotModel->getModelFrame().c_str());

    //Retrieve robot frame
    ROS_INFO("Model frame: %s", RobotModel->getModelFrame().c_str());
    //Get Robot State
    RobotState = new robot_state::RobotState(RobotModel);
    //set the robot state to the default values
    RobotState->setToDefaultValues();


    // Create base_bladder group
    if(!active_arm_chain_.init(robot_, base_name, head_name)) {
        ROS_ERROR("Controller could not use the chain from '%s' to '%s'", base_name.c_str(), head_name.c_str());
        return false;
    }

    base_joint_group = RobotModel->getJointModelGroup(base_group);
    RobotState->copyJointGroupPositions(base_joint_group, base_joint_values);

/*
    // Create passive arm chain.
    if(!passive_arm_chain_.init(robot_, base_name, right_name)) {
        ROS_ERROR("Controller could not use the chain from '%s' to '%s'", base_name.c_str(), right_name.c_str());
        return false;
    }

    // Create KDL chains, solvers, etc.
    // KDL chains.
    passive_arm_chain_.toKDL(passive_arm_fk_chain_);
    active_arm_chain_.toKDL(active_arm_fk_chain_);

    // Pose solvers.
    passive_arm_fk_solver_.reset(new KDL::ChainFkSolverPos_recursive(passive_arm_fk_chain_));
    active_arm_fk_solver_.reset(new KDL::ChainFkSolverPos_recursive(active_arm_fk_chain_));

    // Jacobian sovlers.
    passive_arm_jac_solver_.reset(new KDL::ChainJntToJacSolver(passive_arm_fk_chain_));
    active_arm_jac_solver_.reset(new KDL::ChainJntToJacSolver(active_arm_fk_chain_));

    // Pull out joint states.
    int joint_index;

    // Put together joint states for the active arm.
    joint_index = 1;
    while (true)
    {
        // Check if the parameter for this active joint exists.
        std::string joint_name;
        std::string param_name = std::string("/active_arm_joint_name_" + to_string(joint_index));
        if(!n.getParam(param_name.c_str(), joint_name))
            break;

        // Push back the joint state and name.
        superchick_mechanism_model::JointState* jointState = robot_->getJointState(joint_name);
        active_arm_joint_state_.push_back(jointState);
        if (jointState == NULL)
            ROS_INFO_STREAM("jointState: " + joint_name + " is null");

        active_arm_joint_names_.push_back(joint_name);

        // Increment joint index.
        joint_index++;
    }
    // Validate that the number of joints in the chain equals the length of the active arm joint state.
    if (active_arm_fk_chain_.getNrOfJoints() != active_arm_joint_state_.size())
    {
        ROS_INFO_STREAM("num_fk_chain: " + to_string(active_arm_fk_chain_.getNrOfJoints()));
        ROS_INFO_STREAM("num_joint_state: " + to_string(active_arm_joint_state_.size()));
        ROS_ERROR("Number of joints in the active arm FK chain does not match the number of joints in the active arm joint state!");
        return false;
    }

    // Put together joint states for the passive arm.
    joint_index = 1;
    while (true)
    {
        // Check if the parameter for this passive joint exists.
        std::string joint_name;
        std::string param_name = std::string("/passive_arm_joint_name_" + to_string(joint_index));
        if(!n.getParam(param_name, joint_name))
            break;

        // Push back the joint state and name.
        superchick_mechanism_model::JointState* jointState = robot_->getJointState(joint_name);
        passive_arm_joint_state_.push_back(jointState);
        if (jointState == NULL)
            ROS_INFO_STREAM("jointState: " + joint_name + " is null");
        passive_arm_joint_names_.push_back(joint_name);

        // Increment joint index.
        joint_index++;
    }
    // Validate that the number of joints in the chain equals the length of the active arm joint state.
    if (passive_arm_fk_chain_.getNrOfJoints() != passive_arm_joint_state_.size())
    {
        ROS_INFO_STREAM("num_fk_chain: " + to_string(passive_arm_fk_chain_.getNrOfJoints()));
        ROS_INFO_STREAM("num_joint_state: " + to_string(passive_arm_joint_state_.size()));
        ROS_ERROR("Number of joints in the passive arm FK chain does not match the number of joints in the passive arm joint state!");
        return false;
    }
    // Allocate torques array.
    active_arm_torques_.resize(active_arm_fk_chain_.getNrOfJoints());
    passive_arm_torques_.resize(passive_arm_fk_chain_.getNrOfJoints());
    */

    // Initialize ROS subscribers/publishers, sensors, and position controllers.
    // Note that this must be done after the FK solvers are created, because the sensors
    // will ask to use these FK solvers!
    initialize(n);

    // Tell the PR2 controller manager that we initialized everything successfully.
    return true;
}

// This is called by the controller manager before starting the controller.
void GPSSuperchickPlugin::starting()
{
    // Get current time.
    last_update_time_ = robot_->getTime();
    controller_counter_ = 0;

    // Reset all the sensors. This is important for sensors that try to keep
    // track of the previous state somehow.
    //for (int sensor = 0; sensor < TotalSensorTypes; sensor++)
    for (int sensor = 0; sensor < 1; sensor++)
    {
        sensors_[sensor]->reset(this,last_update_time_);
    }

    // Planning to a Pose goal
    // ^^^^^^^^^^^^^^^^^^^^^^^
/*    geometry_msgs::Pose target_pose1;
    target_pose1.orientation.w = 1.0;
    target_pose1.position.x = 0.28;
    target_pose1.position.y = -0.7;
    target_pose1.position.z = -1.0;
    group->setPoseTarget(target_pose1);*/

    moveit::planning_interface::MoveGroup::Plan my_plan;
    bool success = group->plan(my_plan);

    ROS_INFO("Visualizing plan: (pose goal) %s",success?"":"FAILED");    
    /* Sleep to give Rviz time to visualize the plan. */
    sleep(5.0);

    // Reset position controllers.
    right_bladder_controller_->reset(last_update_time_);
    base_bladder_controller_->reset(last_update_time_);

    // Reset trial controller, if any.
    if (trial_controller_ != NULL) trial_controller_->reset(last_update_time_);
}

// This is called by the controller manager before stopping the controller.
void GPSSuperchickPlugin::stopping()
{
    // Nothing to do here.
}

// This is the main update function called by the realtime thread when the controller is running.
void GPSSuperchickPlugin::update()
{
    // Get current time.
    last_update_time_ = robot_->getTime();

    // Check if this is a controller step based on the current controller frequency.
    controller_counter_++;
    if (controller_counter_ >= controller_step_length_) controller_counter_ = 0;
    bool is_controller_step = (controller_counter_ == 0);

    // Update the sensors and fill in the current step sample.
    update_sensors(last_update_time_,is_controller_step);

    // Update the controllers.
    update_controllers(last_update_time_,is_controller_step);

    // Store the torques.
    for (unsigned i = 0; i < active_arm_joint_state_.size(); i++)
        active_arm_joint_state_[i]->commanded_effort_ = active_arm_torques_[i];

    for (unsigned i = 0; i < passive_arm_joint_state_.size(); i++)
        passive_arm_joint_state_[i]->commanded_effort_ = passive_arm_torques_[i];
}

// Get current time.
ros::Time GPSSuperchickPlugin::get_current_time() const
{
    return last_update_time_;
}

}

/*void GPSSuperchickPlugin::get_task_space_readings(EigenVectorXd &pose, gps::ActuatorType arm) const
{
    if(arm == gps::BASE_BLADDER)
    {
        pose.resize(6);
        for(size_t i = 0; i < pose.size(); ++pose)
        {
            pose(i) = 
        }
    }
}*/

// Register controller to pluginlib
PLUGINLIB_EXPORT_CLASS(gps_agent_pkg, GPSSuperchickPlugin,
                        gps_control::GPSSuperchickPlugin,
                        superchick_controller_interface::Controller)
