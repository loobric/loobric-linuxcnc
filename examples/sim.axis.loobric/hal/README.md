# Loobric Button Integration

This directory contains the HAL configuration and Python script for the Loobric sync button in the LinuxCNC Axis interface.

## Files

- `loobric_button.hal` - HAL configuration for the button and LED
- `loobric_button.py` - Python handler for button press events

## How It Works

1. The button appears in the Axis interface with the label "Loobric Sync"
2. When pressed, it triggers the tool synchronization process
3. The LED lights up when the button is pressed and during sync

## Configuration

The button is configured in `axis.ini` with these key settings:

```ini
[HAL]
HALFILE = hal/loobric_button.hal
POSTGUI_HALFILE = hal/loobric_button.py

[DISPLAY]
USER_LABEL = Loobric Sync
```

## Customization

To modify the button behavior, edit `loobric_button.py`. The main logic is in the `LoobricButton` class.
