#!/usr/bin/env python3
import hal
h = hal.component("pin_lister")
h.ready()
print("Available HAL pins:")
for pin in hal.pins():
    print(f"- {pin}")
h.exit()
