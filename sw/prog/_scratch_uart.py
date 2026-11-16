import sys; sys.path.insert(0,'.')
from sw import loom_asm, loom_model
from sw.protocols import WireWorld, UartBus
from host.loom_host import ModelLink

# --- TX: thread0 at 0 ---
tx = loom_asm.assemble_file('sw/examples/uart_tx.asm')
rx = loom_asm.assemble_file('sw/examples/uart_rx.asm')
loom = loom_model.Loom()
loom.load(tx.words, 0); loom.load(rx.words, 0x80)
loom.set_pc(0, 0); loom.set_pc(1, 0x80)
link = ModelLink(loom)
world = WireWorld(link)
uart = UartBus(world, tx_tid=0, rx_tid=1)
loom.run_threads(0b0011)
world.run(300)
print("pre-run ok, cycle", world.cycle)
uart.app_send(b"Hello")
print("wire bytes:", uart.wire_recv())
print("frames:", [f.cell_text() for f in []])
uart2 = uart
print("app recv (should be empty):", uart.app_recv())
# --- RX: far end transmits to chip ---
uart.wire_send(b"LOOM")
world.run(26000)
print("app recv:", uart.app_recv())
print("wave TX:", uart.waveform(80)['TX P[10]'])
print("wave RX:", uart.waveform(80)['RX P[11]'])
