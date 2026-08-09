# servo.py — 180° positional analog servo
from machine import Pin, PWM

class Servo:
    def __init__(self, pin, freq=50, min_us=500, max_us=2500, max_angle=180):
        """
        pin: GPIO pin number
        freq: PWM frequency in Hz (50Hz standard for analog servos)
        min_us: pulse width (µs) corresponding to 0 degrees
        max_us: pulse width (µs) corresponding to max_angle degrees
        max_angle: full mechanical range of the servo (180 for this model)
        """
        self.pwm = PWM(Pin(pin), freq=freq)
        self.min_us = min_us
        self.max_us = max_us
        self.max_angle = max_angle

    def _write_us(self, us):
        # Clamp to physical pulse-width limits — prevents driving the horn
        # past its mechanical end-stop, which stalls the gear train and
        # draws stall current indefinitely (see "Stall Current at Locked" spec)
        us = max(self.min_us, min(self.max_us, us))
        duty_ns = int(us * 1000)  # µs -> ns for duty_ns()
        self.pwm.duty_ns(duty_ns)

    def move(self, degree):
        """
        degree: target angle, 0-180 (float or int)
        Linearly interpolates degree onto the pulse-width range.
        """
        degree = max(0, min(self.max_angle, degree))
        us = self.min_us + (self.max_us - self.min_us) * (degree / self.max_angle)
        self._write_us(us)

    def stop(self):
        # Disables PWM output — servo loses holding torque (no braking effect,
        # unlike a stepper with detent torque)
        self.pwm.deinit()