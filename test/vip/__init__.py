"""Independent, RTL-free protocol VIPs for the LOOM verification suite.

These are *reference models of the far end of the wire*: pure Python, no cocotb
and no DUT imports.  They are deliberately written independently of
`sw/protocols.py` (the firmware author's application-layer helpers) so that the
two implementations cross-check each other.

Modules
-------
`uart`  -- 8N1 UART: decode (TX side) + schedule (RX side) + framing/baud checks
`spi`   -- SPI modes 0-3: slave model for DUT-master tests, master schedule for
           DUT-target tests, transaction decoder
`i2c`   -- open-drain I2C bus: controller model (DUT-target tests), target model
           (DUT-controller tests), START/STOP/ACK decode, clock stretching
`wave`  -- pure-ASCII waveform renderer shared by the protocol tests
"""

from . import i2c, spi, uart, wave  # noqa: F401
