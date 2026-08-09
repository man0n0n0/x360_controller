# BOOT FILE FOR ESP32 c3

from time import sleep_ms
from machine import Pin
 
led = Pin(8, Pin.OUT)
btn = Pin(9, Pin.IN, Pin.PULL_UP) 

for i in range(3): #blink 3 times
    led.value(1)
    sleep_ms(100)
    led.value(0)
    sleep_ms(100)

if btn.value() == 0 :
    pass

else :
    import main
