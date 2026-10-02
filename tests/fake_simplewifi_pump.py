#!/usr/bin/env python3
"""Simulador da bomba Astral Top +12 (modulo Simple-WiFi) - TCP, protocolo decifrado.
Usado por tests/test_simplewifi_against_fake_pump.py; pode tambem rodar sozinho.
Todos os dados sao FABRICADOS (MAC e valores ficticios; nenhuma senha de Wi-Fi) -
por isso este arquivo e versionado, diferente das capturas reais em docs/astral/.

Comportamento (igual ao observado na bomba real):
  - transmite sozinho ~a cada 0,6 s uma resposta de 4 quadros de 50 bytes, com paginas rotativas
  - responde na hora a um pedido  AA 5A B1 01 + 46 zeros
  - aceita escrita  AA 5A B1 83 01 | MAC | 22 bytes | CRC  (valida CRC); aplica apos --apply-delay s
  - um cliente por vez: nova conexao derruba a anterior
Falhas injetaveis:  --drop-writes N (ignora as N primeiras escritas, em silencio)
                    --reset-after N (fecha a conexao apos N respostas)
                    --apply-delay S (atraso para refletir a escrita; default 1.0)
Uso: python3 tests/fake_simplewifi_pump.py [--port 60000] [--host 127.0.0.1] [opcoes]
"""
import argparse, asyncio, time
MAC = bytes.fromhex("02 00 00 00 00 01")

def crc16(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

def raw(c): return int(round((c + 30) * 2))

def frame(typ, page, payload):
    f = bytes([0xAA, 0x5A, 0xB1, typ, page]) + MAC + bytes(payload)
    f += crc16(f).to_bytes(2, "little")
    return f.ljust(50, b"\0")

class Pump:
    def __init__(self):
        self.on, self.set, self.t_in, self.t_out, self.amb = 1, 33.0, 26.0, 27.0, 21.0
        self.err = b"    "          # " E03" quando sem fluxo
        self.page1 = None
        self.slot = 0
    def body80_1(self):
        return bytes([self.on, 1, 0x0C, 0x74, raw(self.set), 0x74, 0x8C, 0x54, 0x8C, 0x54, 0x8C, 0x54,
                      0x3E, 0x3E, 0, 0, 0, 0, 0, 0, 0, 0])
    def comp(self): return 1 if self.on and self.t_in < self.set else 0
    def slots(self):
        c = self.comp()
        g = lambda gid, data: bytes([gid, len(data), 0x01, len(data)]) + data
        return [
            frame(0x80, 1, self.body80_1()),
            frame(0xD0, 1, bytes([0, raw(self.t_in), raw(self.t_out), raw(self.amb), 0, 2, c | (8 if c else 0), 0, 0, 0] +
                                  list(self.err) + [0, 0, 0, 0, 0, 0x0A, 0x1E, 0, 0, 0])[:22].ljust(22, b"\0")),
            frame(0xD0, 0, g(0x4F, bytes([c, 0, 0, c, 0, 0x92, 0, 0, 0, 0, 0, 0, 0, 0])).ljust(35, b"\0")),
            frame(0xD0, 0, g(0x53, bytes([0, 1 if self.err.strip() else 0, 0, 0, 0, 0, 0, 0])).ljust(35, b"\0")),
            frame(0xD0, 0, g(0x54, bytes([0, raw(self.t_in), raw(self.t_out), raw(18.0), raw(self.amb), 0, 0, 0])).ljust(35, b"\0")),
            frame(0x80, 2, b"FAKE00\0" + b"HeatPump".ljust(28, b"\0")),
            frame(0x80, 3, bytes([0, 17, 0]) + b"Aquecedor de teste".ljust(32, b"\0")),
            frame(0x80, 4, bytes([12]) + b"FAKE00000001" + bytes(8) + bytes([6]) + b"TEST01".ljust(15, b"\0")),
        ]  # nao ha paginas 80/6 e 80/7 (senha) no simulador
    def response(self, n=4):
        s = self.slots(); out = b""
        for _ in range(n):
            out += s[self.slot % len(s)]; self.slot += 1
        return out

async def main(a):
    pump, state = Pump(), {"writer": None, "drops": a.drop_writes, "writes": 0}

    async def apply_later(on, sp):
        await asyncio.sleep(a.apply_delay); pump.on, pump.set = on, sp

    async def handle(r, w):
        if state["writer"]:
            try: state["writer"].transport.abort()
            except Exception: pass
        state["writer"] = w
        sent = 0
        async def push():
            nonlocal sent
            while True:
                w.write(pump.response(4 if sent % 4 else 5)); await w.drain(); sent += 1
                if a.reset_after and sent >= a.reset_after:
                    print("[sim] reset da conexao"); w.transport.abort(); return
                await asyncio.sleep(0.6)
        task = asyncio.create_task(push())
        try:
            buf = b""
            while True:
                d = await r.read(4096)
                if not d: break
                buf += d
                while True:
                    i = buf.find(b"\xaa\x5a\xb1")
                    if i < 0 or len(buf) - i < 4: buf = buf[-3:] if i < 0 else buf[i:]; break
                    buf = buf[i:]
                    if buf[3] == 0x01:
                        if len(buf) < 50: break
                        w.write(pump.response(4)); buf = buf[50:]
                    elif buf[3] == 0x83:
                        if len(buf) < 35: break
                        f, buf = buf[:35], buf[35:]
                        ok = crc16(f[:-2]) == int.from_bytes(f[-2:], "little") and f[5:11] == MAC
                        state["writes"] += 1
                        if not ok: print("[sim] escrita com CRC/MAC invalido: ignorada")
                        elif state["drops"] > 0:
                            state["drops"] -= 1; print("[sim] escrita IGNORADA (falha injetada)")
                        else:
                            body = f[11:33]; print(f"[sim] escrita ok: ligada={body[0]} set={body[4]/2-30}")
                            asyncio.create_task(apply_later(body[0], body[4] / 2 - 30))
                    else: buf = buf[3:]
        except ConnectionError: pass
        finally:
            task.cancel()
    srv = await asyncio.start_server(handle, a.host, a.port)  # already serving; no need for serve_forever()
    print(f"[sim] ouvindo em {a.host}:{a.port}")
    try:
        await asyncio.Event().wait()  # block until cancelled
    except asyncio.CancelledError:
        # Server.serve_forever() itself awaits wait_closed() on cancellation,
        # which can hang once a client has connected and gone (an asyncio.Server
        # quirk, not specific to this script) - avoid serve_forever() entirely
        # and close the listening socket / abort the live connection directly.
        srv.close()
        if state["writer"]:
            try: state["writer"].transport.abort()
            except Exception: pass
        raise

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=60000)
    p.add_argument("--drop-writes", type=int, default=0); p.add_argument("--reset-after", type=int, default=0)
    p.add_argument("--apply-delay", type=float, default=1.0)
    try: asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt: pass
