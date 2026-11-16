; Wave player: stream 16 patterns/s from DMEM. The host preloads DMEM first.
wavedemo:
    li    r6, 0x0000          ; pattern table starts at DMEM word 0
    csrw  WADDR, r6
    li    r7, 16              ; 16 patterns
    csrw  WLEN, r7
    li    r1, 0x00C0          ; 12.0 cycles per pattern (12.4 fixed point)
    csrw  WPER, r1
    li    r2, 0x0003          ; start = 1, loop = 1
    csrw  WCTRL, r2
done:
    halt
