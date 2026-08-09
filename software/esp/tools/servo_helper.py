from servo import Servo
from time import sleep

servo = Servo(pin=20)  # defaults: min_us=500, max_us=2500, max_angle=360

s = 0.1
try:
    for i in range(180):
        print(i)
        servo.move(i)
        sleep(s)
except KeyboardInterrupt:
    print("Keyboard interrupt")
    servo.stop()