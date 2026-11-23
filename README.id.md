# Muse on OpenCode

[English](README.md) | **Bahasa Indonesia**

Jalankan agen personal [Muse](https://muse.ai) kamu sebagai model di dalam [OpenCode](https://opencode.ai) di laptopmu. Muse membaca dan mengedit workspace lokalmu lewat tool bawaan OpenCode, menampilkan blok reasoning per langkah, mencari web dari VM-nya, dan memahami gambar.

Tidak perlu API key dari provider model mana pun. Semuanya jalan di VM Muse milikmu sendiri plus tunnel gratis.

## Cara kerja

```
OpenCode (laptopmu)
  -> tunnel HTTPS (localhost.run, subdomain stabil)
    -> 9Router (VM Muse, 127.0.0.1:20128)
      -> bridge.py (VM Muse, 127.0.0.1:8765)
        -> worker agent Muse (VM Muse, mengambil job dan menjawab)
```

- `bridge.py` menyediakan endpoint `/v1/chat/completions` yang kompatibel dengan OpenAI dan mengantrekan setiap request sebagai file JSON.
- Worker Muse (scheduled task atau hook di dalam Muse) memantau antrean, menjawab dengan teks atau `tool_calls`, dan mengirim catatan progress serta reasoning selama bekerja.
- OpenCode mengeksekusi tool call secara lokal di laptopmu lalu mengirim hasilnya kembali. Filemu tidak pernah meninggalkan laptop; yang lewat tunnel hanya percakapannya.
- 9Router berada di depan bridge sebagai gateway API publik (API key, routing model).

## Fitur

- **Agen workspace**: baca, edit, tulis file dan jalankan perintah shell lewat tool OpenCode, dibatch sampai 8 call per langkah.
- **Blok reasoning**: rencana dan temuan worker muncul sebagai blok thinking di OpenCode, per langkah.
- **Pencarian web**: pertanyaan soal informasi terkini dicari oleh worker dari VM, lengkap dengan sumber. Tidak perlu API key pencarian pihak ketiga di laptopmu.
- **Gambar**: lampirkan screenshot atau gambar di OpenCode; worker akan melihatnya.
- **Konteks 1M**: config contoh mendeklarasikan context window 1.000.000 token.
- **URL stabil**: subdomain localhost.run berbasis SSH key yang bertahan melewati reconnect dan restart VM, jadi `opencode.json` kamu tidak pernah berubah.

## Kebutuhan

- Akun Muse dengan VM-nya (di sinilah Muse berjalan).
- Laptop dengan Node.js (untuk OpenCode) dan SSH.
- Akun gratis [localhost.run](https://localhost.run) (untuk subdomain tunnel yang stabil).

## Setup

Langkah 1 sampai 4 semuanya berjalan **di dalam VM Muse**. Minta Muse mengerjakannya untukmu, atau jalankan perintahnya sendiri di shell VM.

### 1. Install bridge

```bash
mkdir -p ~/muse-bridge/queue
cp bridge.py ~/muse-bridge/
cd ~/muse-bridge

# Buat dua API key. Setiap key dicetak SEKALI. Simpan ke file dengan permission 600.
python3 bridge.py keygen --role worker --label muse-vm
python3 bridge.py keygen --role user   --label 9router
```

Jalankan sebagai service systemd (`/etc/systemd/system/muse-bridge.service`):

```ini
[Unit]
Description=Muse bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/<user>/muse-bridge/bridge.py
Environment=BRIDGE_QUEUE=/home/<user>/muse-bridge/queue
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl daemon-reload && systemctl enable --now muse-bridge
curl http://127.0.0.1:8765/health   # -> {"ok": true}
```

### 2. Install 9Router

```bash
npm config set prefix ~/.npm-global
npm install -g 9router
```

Jalankan sebagai service systemd (`/etc/systemd/system/9router.service`). Atur password dashboard yang kuat lewat `INITIAL_PASSWORD` di file env (permission 600):

```ini
[Unit]
Description=9Router
After=network.target

[Service]
ExecStart=/home/<user>/.npm-global/bin/9router --port 20128 --host 127.0.0.1 --no-browser --skip-update
EnvironmentFile=/home/<user>/muse-bridge/9router.env
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

`9router.env` berisi satu baris: `INITIAL_PASSWORD=<password kuat yang kamu buat>`.

```bash
systemctl daemon-reload && systemctl enable --now 9router
```

### 3. Daftarkan bridge di 9Router

Buka dashboard 9Router (forward port 20128 atau pakai tunnel dari langkah 4 dulu), lalu:

1. **Provider node**: prefix `musevm`, API type `chat`, base URL `http://127.0.0.1:8765/v1`.
   - Prefix tidak boleh `muse`; nama itu bertabrakan dengan provider Muse bawaan 9Router dan merusak pencarian kredensial.
2. **Connection**: API key = key **user** bridge dari langkah 1, default model `muse-spark`.
   - Nama model itu penting: 9Router menentukan dukungan vision dengan mencocokkan nama model ke pola. `muse-spark` cocok dengan pola vision (konteks 1M). Nama polos `muse` tidak cocok, dan gambar akan dibuang dengan pesan "[image omitted: model has no vision support]".
3. **Combo**: nama `muse`, models `["musevm/muse-spark"]`. Ini nama model publik yang akan dipakai OpenCode.
4. **API key**: buat satu untuk laptopmu (misalnya bernama `opencode-laptop`). Biarkan login dashboard dan kewajiban API key tetap aktif.

### 4. Tambahkan worker Muse

Worker adalah scheduled task atau hook Muse yang bangun saat ada job pending, mengambilnya dari bridge, lalu menjawab. Pakai prompt di [`worker-prompt.md`](worker-prompt.md) apa adanya, sesuaikan hanya path file key-nya.

Dua aturan yang menentukan berhasil tidaknya setup ini:

- Worker harus menjawab pertanyaan workspace dengan `tool_calls` (OpenCode mengeksekusinya di laptopmu), bukan dengan membaca file di dalam VM.
- Worker membaca API key-nya dari file berpermission 600 hanya di dalam perintah shell-nya. Nilai key tidak boleh muncul di chat, log, atau repo.

Poll antrean tiap 5 detik (hook) plus prompt worker sudah cukup. Fallback terjadwal tiap 1 menit itu opsional; ia membakar token saat idle.

### 5. Ekspos 9Router dengan tunnel stabil

Domain gratis localhost.run berganti-ganti. SSH key yang terdaftar mengikat subdomain yang stabil:

1. Buat akun gratis di [admin.localhost.run](https://admin.localhost.run) dan upload SSH public key VM (`~/.ssh/id_ed25519.pub`).
2. Jalankan tunnel dengan autentikasi key (tanpa user `nokey`):

```bash
ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes -o ServerAliveInterval=30 \
  -R 80:localhost:20128 localhost.run
```

Jalankan sebagai service systemd (`muse-tunnel`) dengan `Restart=always`. URL yang tercetak (misalnya `https://<subdomain>.app.lhr.life`) tetap sama melewati reconnect.

> Jika VM kamu hanya bisa ke internet lewat HTTP CONNECT proxy, tambahkan `ProxyCommand` yang meneruskan SSH lewat proxy itu. Di kebanyakan setup, egress langsung sudah cukup.

### 6. Konfigurasi OpenCode di laptop

Install OpenCode:

```bash
npm install -g opencode-ai
```

Salin [`examples/opencode.json`](examples/opencode.json) ke `~/.config/opencode/opencode.json` (Windows: `C:\Users\<username>\.config\opencode\opencode.json`) dan ganti dua placeholder-nya:

- `<YOUR-TUNNEL-URL>`: URL localhost.run stabilmu dari langkah 5.
- `<YOUR-9ROUTER-API-KEY>`: API key dari langkah 3.

Tes endpoint-nya sebelum membuka OpenCode:

```bash
curl <YOUR-TUNNEL-URL>/v1/chat/completions \
  -H "Authorization: Bearer <YOUR-9ROUTER-API-KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"muse","stream":false,"messages":[{"role":"user","content":"Say hello in one word."}]}'
```

Lalu jalankan `opencode`, pilih model `muse-vm/muse`, dan minta ia membaca file di workspace-mu.

## Catatan pemakaian

- **Tool call berjalan di laptopmu.** Sistem permission OpenCode adalah gerbangnya. Config contoh mengizinkan otomatis tool read-only (`read`, `glob`, `grep`, `webfetch`); edit dan perintah shell tetap bertanya.
- **Pertanyaan web** dicari oleh worker dari VM. Tool `websearch` bawaan OpenCode butuh key pihak ketiga (Exa) dan sengaja tidak dipakai.
- **Gambar** bekerja lewat nama model `muse-spark`. Lampirkan screenshot lalu tanyakan isinya.
- **Subagent dan tool MCP** diteruskan sebagai tool call biasa. Subagent berjalan di laptopmu dan memanggil balik lewat bridge yang sama, jadi tetap bekerja tapi menambah latensi antrean.

## Batasan

Batas jujur dari desain ini:

- **Latensi**: setiap langkah memakan satu kali worker bangun, kira-kira 15 sampai 40 detik. Batching (sampai 8 tool call per langkah) membuat tugas multi-file tetap praktis.
- **Tidak ada streaming token**: reasoning dan jawaban datang per blok, bukan token per token. Catatan progress membuat UI tetap informatif selama menunggu.
- **Satu worker**: sesi OpenCode yang paralel mengantre di belakang satu sama lain.
- **Biaya token**: setiap job adalah satu run agen Muse di VM kamu. Sesi panjang biayanya sebanding.

## Troubleshooting

| Gejala | Penyebab dan perbaikan |
|---|---|
| `401` dari URL tunnel | API key 9Router di `opencode.json` salah atau kosong. |
| Connection error setelah VM restart | Tunnel mati atau URL berganti. Pakai autentikasi key (langkah 5) untuk URL stabil; cek `systemctl status muse-tunnel`. |
| "Missing API key" di OpenCode | Key harus tertanam di `opencode.json` (seperti contoh) atau diekspor di shell yang sama dengan yang menjalankan OpenCode. |
| Model hanya membalas teks, tidak pernah membaca file | Worker tidak mengembalikan `tool_calls`. Cek prompt worker (langkah 4) dan pastikan request menyertakan `tools`. |
| "[image omitted: model has no vision support]" | Nama model upstream tidak cocok dengan pola vision di 9Router. Pakai `muse-spark` (langkah 3). |
| `429 Muse bridge busy` | Lebih dari 10 job pending. Tunggu worker mengosongkan antrean. |
| Jawaban timeout pada tugas panjang | Bridge menunggu sampai 600 detik per request. Pecah tugas yang sangat panjang jadi permintaan yang lebih kecil. |

## Keamanan

- Jangan pernah commit key. `keys.json`, file env, dan `opencode.json` berisi key asli tetap di luar git (permission 600).
- Tunnel mengekspos halaman login 9Router. Pakai password dashboard yang kuat dan biarkan kewajiban API key aktif.
- Bridge sendiri hanya mendengarkan di `127.0.0.1`; yang ditunnel hanya 9Router.
- Key worker dan key user adalah role terpisah. Rotasi dengan `bridge.py keydel` + `keygen` jika ada yang bocor.

## Lisensi

MIT. Lihat [LICENSE](LICENSE).
