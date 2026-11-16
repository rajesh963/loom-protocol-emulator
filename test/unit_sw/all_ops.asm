; all_ops.asm — every LOOM mnemonic once, assembles to a known image.
; Used by test/unit_sw/test_asm.py as the full-coverage round-trip program.

.equ MASK, 0xFCFF
.equ SHIFT, 3

start:          movi    r0, 0x1234      ; 0x04001234
                halt                    ; 0x02000000
                mov     r1, r0          ; 0x04800000
                add     r1, r2
                addi    r1, 1
                sub     r1, r2
                subi    r1, 0x10
                and     r1, r2
                andi    r1, MASK
                or      r1, r2
                ori     r1, 0x0F
                xor     r1, r2
                xori    r1, 0xF0
                inv     r1
                shl     r1, SHIFT
                shr     r1, 3
                rol     r1, 4
                ror     r1, 4
                cmp     r1, r2
                cmpi    r1, 7
                csrw    TPERIOD, r0
                csrr    r0, TSTATUS
                out     0x0400
                set     0x0001
                clr     0x0002
                tog     0x0004
                in      r0
                dir     0xAA
                dset    0x0F
                dclr    0xF0
                setp    0x208D
                setm    MASK
                setem   0x0300
                setec   0x0100
                movi    r1, 0x0000
                emit    r1
                emita   r1, 100
                emitd   r1, 5
                emite   r1
                inat    r1, 42
                waitu   42
                waitd   0x208D
                waitp   0x1201
                ld      r0, 0x10
                st      0x11, r0
                ldi     r0, r1
                sti     r1, r2
                push    r0
                pull    r0
                pushi   0x55
                jmp     next
next:           jz      done
                jnz     next
                jc      next
                jnc     next
                rpt     9
                evset   1
                evwait  2
                irq     7
done:           nop
                rpt     3
                add     r0, r1
                .word   0xDEADBEEF, start
                .align  8
                .fill   3, 0xCAFEF00D
                li      r3, -1
                .equ    NEG, -2
                movi    r4, NEG
