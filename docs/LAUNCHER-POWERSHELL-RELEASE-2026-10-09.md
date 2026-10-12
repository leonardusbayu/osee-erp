# Peluncur OSEE lintas PowerShell

Masalah yang direproduksi: PowerShell 7.5+ mengubah timestamp ISO dari JSON menjadi `DateTime`. Akibatnya Stop menolak proses OSEE yang benar walaupun PID, waktu mulai, executable dan command masih cocok. Windows PowerShell 5.1 tidak melakukan konversi tersebut.

Peluncur sekarang mendeteksi dukungan `ConvertFrom-Json -DateKind String` dan memakainya bila tersedia. Representasi timestamp tetap persis; pemeriksaan identitas proses dan native handle sebelum penghentian tetap berlaku. Tidak ada proses perusahaan atau aplikasi lain dihentikan untuk QA ini.

Enam kasus integrasi lulus pada Windows PowerShell 5.1.22000.2538 dan PowerShell 7.6.5: reuse/status/stop lintas kedua arah, port sehat milik aplikasi lain, command asing, timestamp berbeda satu tick, dan PID asing pada state palsu. Fixture memakai server HTTP sementara serta folder/venv terpisah dan dibersihkan setelah uji. Ruff untuk dua file peluncur lulus. Pemeriksaan `-Status` pada aplikasi nyata melalui PowerShell 7 juga berhasil dan menunjukkan port 8001; itu pemeriksaan baca saja.

Bukti komponen akhir: `.local/fix-2026-10-02/verification/launcher-cross-powershell-final-20261009-3.*`. Selama uji ini katalog sumber identitas pajak sedang dikerjakan dalam batch berikutnya, sehingga hash seluruh repository berubah. Bukti enam kasus ini hanya untuk dua file peluncur yang tetap sama sepanjang uji:

- `scripts/start.ps1`: `8ce85dddc17b3c3bfa446bf6b5ba664ed7d85b1757e270041a3dc0cd92644b9c`.
- `scripts/tests_launcher.py`: `f284649f08b7af6ba6c1d4abf97632e6274c617f5ede52d8bd4b8efac62b720d`.

Hasil bulanan pada build `c442aa16…` tetap bukti historis untuk batch bulanan; laporan ini tidak mengklaim seluruh suite pajak dijalankan ulang pada perubahan launcher atau batch identitas yang belum selesai.
