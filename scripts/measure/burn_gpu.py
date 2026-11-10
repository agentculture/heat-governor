#!/usr/bin/env python3
"""GPU burn: wgpu (Vulkan) compute shader FMA loop; logs GFLOPS every ~5 s. argv: log hgm-burn"""

import sys
import time

import numpy as np
import wgpu

ITERS = 4096
N = 1 << 20  # invocations per dispatch
SHADER = f"""
@group(0) @binding(0) var<storage, read_write> buf: array<vec4<f32>>;
@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {{
    var a = buf[id.x];
    var b = a * 0.999 + vec4<f32>(0.001);
    var c = b * 1.001;
    var d = a + b;
    for (var i = 0u; i < {ITERS}u; i++) {{
        a = fma(a, b, c); b = fma(b, c, d); c = fma(c, d, a); d = fma(d, a, b);
    }}
    buf[id.x] = a + b + c + d;
}}
"""
adapter = wgpu.gpu.request_adapter_sync(power_preference="high-performance")
dev = adapter.request_device_sync()
print("adapter:", adapter.info, file=sys.stderr)
data = np.random.rand(N * 4).astype(np.float32)
buf = dev.create_buffer_with_data(
    data=data, usage=wgpu.BufferUsage.STORAGE | wgpu.BufferUsage.COPY_SRC
)
pipe = dev.create_compute_pipeline(
    layout="auto", compute={"module": dev.create_shader_module(code=SHADER), "entry_point": "main"}
)
bg = dev.create_bind_group(
    layout=pipe.get_bind_group_layout(0),
    entries=[{"binding": 0, "resource": {"buffer": buf, "offset": 0, "size": buf.size}}],
)
flops = N * ITERS * 4 * 4 * 2  # 4 fma x vec4 x 2 flops
log = open(sys.argv[1], "a", buffering=1)
while True:
    t0, k = time.time(), 0
    while time.time() - t0 < 5:
        enc = dev.create_command_encoder()
        p = enc.begin_compute_pass()
        p.set_pipeline(pipe)
        p.set_bind_group(0, bg)
        p.dispatch_workgroups(N // 256)
        p.end()
        dev.queue.submit([enc.finish()])
        dev.queue.read_buffer(buf, 0, 16)  # sync
        k += 1
    dt = time.time() - t0
    log.write(f"{time.time():.1f} gpu_gflops {flops * k / dt / 1e9:.1f}\n")
