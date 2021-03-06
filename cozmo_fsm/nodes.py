import time
import inspect
import random
from math import sqrt

import cozmo
from cozmo.util import distance_mm, speed_mmps, degrees, Distance, Angle

from .base import *
from .events import *

#________________ Ordinary Nodes ________________

class ParentCompletes(StateNode):
    def start(self,event=None):
        super().start(event)
        if TRACE.trace_level > TRACE.statenode_startstop:
            print('TRACE%d:' % TRACE.statenode_startstop,
                  '%s is causing %s to complete' % (self, self.parent))
        if self.parent:
            self.parent.post_completion()

class ParentSucceeds(StateNode):
    def start(self,event=None):
        super().start(event)
        if TRACE.trace_level > TRACE.statenode_startstop:
            print('TRACE%d:' % TRACE.statenode_startstop,
                  '%s is causing %s to succeed' % (self, self.parent))
        if self.parent:
            self.parent.post_success()

class ParentFails(StateNode):
    def start(self,event=None):
        super().start(event)
        if TRACE.trace_level > TRACE.statenode_startstop:
            print('TRACE%d:' % TRACE.statenode_startstop,
                  '%s is causing %s to fail' % (self, self.parent))
        if self.parent:
            self.parent.post_failure()

class MoveLift(StateNode):
    def __init__(self,speed):
        super().__init__()
        self.speed = speed

    def start(self,event=None):
        if self.running: return
        super().start(event)
        self.robot.move_lift(self.speed)

    def stop(self):
        if not self.running: return
        self.robot.move_lift(0)
        super().stop()

#________________ Coroutine Nodes ________________

class CoroutineNode(StateNode):
    def __init__(self):
        super().__init__()
        self.handle = None

    def start(self,event=None):
        super().start(event)
        cor = self.coroutine_launcher()
        if inspect.iscoroutine(cor):
            self.handle = self.robot.loop.create_task(cor)
        else:
            print('cor=',cor,'type=',type(cor))
            raise ValueError("Result of %s launch_couroutine() is %s, not a coroutine." %
                             (self,cor))

    def coroutine_launcher(self):
        raise Exception('%s lacks a coroutine_launcher() method' % self)
    
    def stop(self):
        if not self.running: return
        if self.handle: self.handle.cancel()
        super().stop()


class DriveWheels(CoroutineNode):
    def __init__(self,l_wheel_speed,r_wheel_speed,**kwargs):
        super().__init__()
        self.l_wheel_speed = l_wheel_speed
        self.r_wheel_speed = r_wheel_speed
        self.kwargs = kwargs

    def coroutine_launcher(self):
        return self.robot.drive_wheels(self.l_wheel_speed,self.r_wheel_speed,**self.kwargs)

    def stop_wheels(self):
        try:
            driver = self.robot.drive_wheels(0,0)
            # driver is either a co-routine or None
            if driver: driver.send(None)  # will raise StopIteration
        except StopIteration: pass

    def stop(self):
        if not self.running: return
        self.stop_wheels()
        super().stop()        


class DriveForward(DriveWheels):
    def __init__(self, distance=50, speed=50, **kwargs):
        if isinstance(distance, cozmo.util.Distance):
            distance = distance.distance_mm
        if isinstance(speed, cozmo.util.Speed):
            speed = speed.speed_mmps
        if distance < 0:
            distance = -distance
            speed = -speed
        self.distance = distance
        self.speed = speed
        self.kwargs = kwargs
        super().__init__(speed,speed,**self.kwargs)
        self.polling_interval = 0.1

    def start(self,event=None):
        if self.running: return
        self.start_position = self.robot.pose.position
        super().start(event)

    def poll(self):
        """See how far we've traveled"""
        p0 = self.start_position
        p1 = self.robot.pose.position
        diff = (p1.x - p0.x, p1.y - p0.y)
        dist = sqrt(diff[0]*diff[0] + diff[1]*diff[1])
        if dist >= self.distance:
            self.poll_handle.cancel()
            self.stop_wheels()
            self.post_completion()

class DriveTurn(DriveWheels):
    def __init__(self, angle=90, speed=50, **kwargs):
        if isinstance(angle, cozmo.util.Angle):
            angle = angle.degrees
        if isinstance(speed, cozmo.util.Speed):
            speed = speed.speed_mmps
        if angle < 0:
            speed = -speed
        self.angle = angle
        self.speed = speed
        self.kwargs = kwargs
        super().__init__(-speed,speed,**self.kwargs)
        self.polling_interval = 0.05

    def start(self,event=None):
        if self.running: return
        super().start(event)
        self.last_heading = self.robot.pose.rotation.angle_z.degrees
        self.traveled = 0

    def poll(self):
        """See how far we've traveled"""
        p0 = self.last_heading
        p1 = self.robot.pose.rotation.angle_z.degrees
        self.last_heading = p1
        # Assume we're polling quickly enough that diff will be small;
        # typically only about 1 degree.  So diff will be large only
        # if the heading has passed through 360 degrees since the last
        # call to poll().  Use 90 degrees as an arbitrary large threshold.
        diff = p1 - p0
        if diff  < -90.0:
            diff += 360.0
        elif diff > 90.0:
            diff -= 360.0
        self.traveled += diff
        if abs(self.traveled) > abs(self.angle):
            self.poll_handle.cancel()
            self.stop_wheels()
            self.post_completion()

#________________ Action Nodes ________________

class ActionNode(StateNode):
    relaunch_delay = 0.050 # 50 milliseconds

    def __init__(self, abort_on_stop=True):
        """Call this method only after the subclass __init__ has set
        up self.action_kwargs"""
        self.abort_on_stop = abort_on_stop
        super().__init__()
        if 'in_parallel' not in self.action_kwargs:
            self.action_kwargs['in_parallel'] = True
        self.cozmo_action_handle = None

    def start(self,event=None):
        super().start(event)
        self.launch_or_retry()

    def launch_or_retry(self):
        try:
            result = self.action_launcher()
        except cozmo.exceptions.RobotBusy:
            if TRACE.trace_level >= TRACE.statenode_startstop:
                print('TRACE%d:' % TRACE.statenode_startstop, self, 'launch_action raised RobotBusy')
            self.handle = self.robot.loop.call_later(self.relaunch_delay, self.launch_or_retry)
            return
        if isinstance(result, cozmo.action.Action):
            self.cozmo_action_handle = result
        else:
            raise ValueError("Result of %s launch_action() is %s, not a cozmo.action.Action." %
                             (self,result))
        self.post_when_complete()

    def action_launcher(self):
        raise Exception('%s lacks an action_launcher() method' % self)
    
    def post_when_complete(self):
       self.robot.loop.create_task(self.wait_for_completion())

    async def wait_for_completion(self):
        async_task = self.cozmo_action_handle.wait_for_completed()
        await async_task
        if TRACE.trace_level >= TRACE.await_satisfied:
            print('TRACE%d:' % TRACE.await_satisfied, self,
                  'await satisfied:', self.cozmo_action_handle)
        # check status for 'completed'; if not, schedule relaunch or post failure
        if self.running:
            if self.cozmo_action_handle.state == 'action_succeeded':
                self.post_completion()
            elif self.cozmo_action_handle.failure_reason[0] == 'cancelled':
                print('CANCELLED: ***>',self,self.cozmo_action_handle)
                self.post_completion()
            elif self.cozmo_action_handle.failure_reason[0] == 'retry':
                print("*** ACTION %s FAILED WITH CODE 'retry': TREATING AS COMPLETE" %
                      self.cozmo_action_handle)
                self.post_completion()
            else:
                self.post_failure(self.cozmo_action_handle)

    def stop(self):
        if not self.running: return
        if self.cozmo_action_handle and self.abort_on_stop and \
                self.cozmo_action_handle.is_running:
            self.cozmo_action_handle.abort()
        super().stop()


class Say(ActionNode):
    """Speaks some text, then posts a completion event."""
    def __init__(self, text="I'm speechless",
                 abort_on_stop=False, **action_kwargs):
        self.text = text
        self.action_kwargs = action_kwargs
        super().__init__(abort_on_stop)

    def start(self,event=None):
        if self.running: return
        utterance = self.text
        if isinstance(event,DataEvent):
                utterance = event.data
        if isinstance(utterance, (list,tuple)):
            utterance = random.choice(utterance)
        if not isinstance(utterance, str):
            utterance = repr(utterance)
        self.utterance = utterance
        super().start(event)
        print("Speaking: '",utterance,"'",sep='')

    def action_launcher(self):
        return self.robot.say_text(self.utterance,**self.action_kwargs)


class Forward(ActionNode):
    def __init__(self, distance=distance_mm(50),
                 speed=speed_mmps(50), abort_on_stop=True, **action_kwargs):
        if isinstance(distance, (int,float)):
            distance = distance_mm(distance)
        elif not isinstance(distance, cozmo.util.Distance):
            raise ValueError('%s distance must be a number or a cozmo.util.Distance' % self)
        if isinstance(speed, (int,float)):
            speed = speed_mmps(speed)
        elif not isinstance(speed, cozmo.util.Speed):
            raise ValueError('%s speed must be a number or a cozmo.util.Speed' % self)
        self.distance = distance
        self.speed = speed
        if 'should_play_anim' not in action_kwargs:
            action_kwargs['should_play_anim'] = False
        self.action_kwargs = action_kwargs
        # super's init must come last because it checks self.action_kwargs
        super().__init__(abort_on_stop)

    def action_launcher(self):
        return self.robot.drive_straight(self.distance, self.speed,
                                         **self.action_kwargs)


class Turn(ActionNode):
    def __init__(self, angle=degrees(90), abort_on_stop=True, **action_kwargs):
        if isinstance(angle, (int,float)):
            angle = degrees(angle)
        elif not isinstance(angle, cozmo.util.Angle):
            raise ValueError('%s angle must be a number or a cozmo.util.Angle' % self)
        self.angle = angle
        self.action_kwargs = action_kwargs
        super().__init__(abort_on_stop)

    def action_launcher(self):
        return self.robot.turn_in_place(self.angle, **self.action_kwargs)

class SetHeadAngle(ActionNode):
    def __init__(self, angle=degrees(0), abort_on_stop=True, **action_kwargs):
        if isinstance(angle, (int,float)):
            angle = degrees(angle)
        elif not isinstance(angle, cozmo.util.Angle):
            raise ValueError('%s angle must be a number or a cozmo.util.Angle' % self)
        self.angle = angle
        self.action_kwargs = action_kwargs
        super().__init__(abort_on_stop)

    def action_launcher(self):
        return self.robot.set_head_angle(self.angle, **self.action_kwargs)


#________________ Animations ________________


class AnimationNode(ActionNode):
    def __init__(self, anim_name='anim_bored_01', **kwargs):
        self.anim_name = anim_name
        self.kwargs = kwargs
        super().__init__()

    def action_launcher(self):
        return self.robot.play_anim(self.anim_name)

class AnimationTriggerNode(ActionNode):
    def __init__(self, trigger=cozmo.anim.Triggers.CubePouncePounceNormal, **kwargs):
        if not isinstance(trigger, cozmo.anim._AnimTrigger):
            raise TypeError('%s is not an instance of cozmo.anim._AnimTrigger' %
                            repr(trigger))
        self.trigger = trigger
        self.kwargs = kwargs
        super().__init__()

    def action_launcher(self):
        return self.robot.play_anim_trigger(self.trigger)

#________________ Behaviors ________________

class StartBehavior(StateNode):
    def __init__(self, behavior=None, stop_on_exit=True):
        if not isinstance(behavior, cozmo.behavior._BehaviorType):
            raise ValueError("'%s' isn't an instance of cozmo.behavior._BehaviorType" %
                             repr(behavior))
        self.behavior = behavior
        self.behavior_handle = None
        self.stop_on_exit = stop_on_exit
        super().__init__()

    def __repr__(self):
        if self.behavior_handle:
            return '<%s %s active=%s>' % \
                   (self.__class__.__name__, self.name, self.behavior_handle.is_active)
        else:
            return super().__repr__()        

    def start(self,event=None):
        if self.running: return
        super().start(event)
        try:
            if self.robot.behavior_handle:
                self.robot.behavior_handle.stop()
        except: pass
        finally:
            self.robot.behavior_handle = None
        self.behavior_handle = self.robot.start_behavior(self.behavior)
        self.robot.behavior_handle = self.behavior_handle
        self.post_completion()

    def stop(self):
        if not self.running: return
        if self.stop_on_exit and self.behavior_handle is self.robot.behavior_handle:
            self.robot.behavior_handle.stop()
            self.robot.behavior_handle = None
        super().stop()

class StopBehavior(StateNode):
    def start(self,event=None):
        if self. running: return
        super().start(event)
        try:
            if self.robot.behavior_handle:
                self.robot.behavior_handle.stop()
        except: pass
        self.robot.behavior_handle = None
        self.post_completion()

class FindFaces(StartBehavior):
    def __init__(self,stop_on_exit=True):
        super().__init__(cozmo.robot.behavior.BehaviorTypes.FindFaces,stop_on_exit)

class KnockOverCubes(StartBehavior):
    def __init__(self,stop_on_exit=True):
        super().__init__(cozmo.robot.behavior.BehaviorTypes.KnockOverCubes,stop_on_exit)

class LookAroundInPlace(StartBehavior):
    def __init__(self,stop_on_exit=True):
        super().__init__(cozmo.robot.behavior.BehaviorTypes.LookAroundInPlace,stop_on_exit)

class PounceOnMotion(StartBehavior):
    def __init__(self,stop_on_exit=True):
        super().__init__(cozmo.robot.behavior.BehaviorTypes.PounceOnMotion,stop_on_exit)

class RollBlock(StartBehavior):
    def __init__(self,stop_on_exit=True):
        super().__init__(cozmo.robot.behavior.BehaviorTypes.RollBlock,stop_on_exit)

class StackBlocks(StartBehavior):
    def __init__(self,stop_on_exit=True):
        super().__init__(cozmo.robot.behavior.BehaviorTypes.StackBlocks,stop_on_exit)
