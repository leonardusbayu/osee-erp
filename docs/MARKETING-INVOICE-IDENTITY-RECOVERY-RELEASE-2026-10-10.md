# Koreksi pelanggan dan produk dari invoice keliru

MKT-02 sudah diterapkan dan diperiksa pada aplikasi lokal, source `d7c6b14a7f4c9adb4216127e94128c5aa6c991036c30da471d29fc41116c978b`. Gate `marketing-identity-final-3` menghasilkan **READY_FOR_SCOPE** untuk kemampuan ini. Penerimaan seluruh ERP, cloud, integrasi live dan UAT staf tetap terbuka pada [status aplikasi](FULL-APP-STATUS-2026-10-09.md).

## Cara kerja untuk staf

Owner dan Finance membuka detail calon pelanggan, memilih koreksi sumber invoice, lalu mengikuti tiga langkah: membandingkan pelanggan/produk lama dan baru, memilih penggantian atau pelepasan sumber, kemudian mengisi alasan/bukti dan mengonfirmasi. Riwayat menampilkan sumber pertama serta nilai sebelum dan sesudah. Marketing dapat melihat sumber dan riwayat, tetapi tidak dapat menjalankan koreksi ini. Hak akses organisasi dan keaktifan akun diperiksa kembali saat penyimpanan.

Hanya identitas yang dapat dibuktikan berasal dari inferensi invoice yang dapat dipulihkan. Nilai yang diisi manual atau dari kampanye tetap dilindungi. Hubungan Order yang perlu dikoreksi menampilkan langkah Operations yang sesuai; sistem tidak melepasnya diam-diam. Invoice, pelunasan, jurnal, pajak, kas dan peserta tidak dipindahkan ke sumber pengganti. Uang yang diterima untuk invoice A tetap pembayaran A.

Jika sumber berubah setelah halaman dibuka, penyimpanan ditolak dengan masukan staf tetap terlihat. Staf memeriksa ulang sumber sebelum mengonfirmasi kembali. Permintaan ulang yang sama tidak membuat keputusan ganda. KPI, evaluasi dan saran AI lama ditandai tidak lagi memakai sumber terbaru, termasuk saat penggantian bernilai sama atau sumber dilepas lalu ditautkan kembali. Data bukti pribadi tidak ditambahkan ke konteks OpenRouter.

## Bukti pengujian

| Pemeriksaan | Hasil pada source yang sama | Batas |
|---|---|---|
| Reproduksi publik Owner/Finance | Dua kasus yang sebelumnya404 kini lulus melalui rute koreksi asli | Sumber inferensi dibuat lewat alur normal |
| Tes terkait SQLite | 68 kasus:62 lulus,6 khusus PostgreSQL dilewati | Skip tidak dihitung sebagai lulus |
| Tes terkait PostgreSQL | 68 lulus tanpa skip, termasuk enam persaingan transaksi nyata | Bukan pengulangan seluruh suite PostgreSQL |
| Suite penuh SQLite | 992 kasus:944 lulus,48 khusus PostgreSQL dilewati | Kasus PostgreSQL di luar68 terkait belum diulang dalam batch ini |
| Kualitas | Lima pemeriksaan lulus | Tidak menjamin seluruh ERP bebas bug |
| Chrome desktop dan ponsel | 10 alur,76 keadaan; tanpa error JavaScript atau overflow horizontal | Data sintetis; bukan pengamatan staf pertama kali |
| Aplikasi perusahaan lokal | 18 pemeriksaan HTTP/aset dan identitas proses lulus | Anonim; rute bisnis diperiksa redirect login, alur berwenang diuji pada fixture terpisah |

Browser menguji penggantian tersimpan, pelepasan ke nilai kosong yang tetap dapat dipulihkan, riwayat, sumber stale, dependensi Order dan akses Marketing yang hanya membaca. Empat respons400 berasal dari konflik yang sengaja diuji; tidak ada5xx. Pembayaran dan dokumen fixture asli diperiksa setelah setiap mutasi. Seluruh provider dimatikan pada QA.

Kegagalan awal tetap tersimpan: rute404, kesalahan analytics, penerimaan asal audit lama yang ambigu/berasal dari actor organisasi lain, serta pilihan radio yang sama-sama ditandai wajib. Perbaikan UI meminta **Pilih satu**. Kesalahan fixture dan pengemasan gate dicatat terpisah dari temuan produk; gate sebelumnya tidak diganti menjadi lulus.

## Preservasi dan restart lokal

Backup dan receipt asli `20261009T191217Z` dipertahankan. Migrasi Marketing0010 membuat register riwayat kosong. Seluruh **980 baris asli pada124 tabel**, metadata kolom, schema yang tidak terkait, waktu migrasi lama, dokumen/direktori, grant/anggota dan fakta Finance/Tax/Operations/Lead tetap sama. Tambahan hanya lima definisi framework dan satu catatan migrasi; tidak ada transaksi atau riwayat bisnis baru. Receipt preservasi SHA256 `2864e059d81f7f37813047443ab4b8ce28303de9292bb168e4c38b6f9dcd9c30`.

Restart pertama tidak menyelesaikan laporan identitas proses. Koordinator mencatat `TimeoutExpired`45 detik dan tertahan saat menunggu pipe keluaran; ia baru keluar setelah keluarga proses milik batch dihentikan. Kegagalan cabang awal tidak dapat dipastikan dari output asli yang tersedia. Proses dihentikan dengan verifikasi PID, waktu lahir, executable, command dan parent; PostgreSQL sistem tetap hidup. Uji fungsi terpisah menemukan penggantian berkas dengan `$null` gagal pada Windows PowerShell; `NullString` lulus pada replay fungsi yang sama.

Restart berikutnya menggunakan keluaran berkas sehingga error sebenarnya terlihat: helper mencoba membaca proses konsol Windows sebagai server. Reproduksi native membuktikan console ditolak dan Python aplikasi diterima. Server yang sudah berjalan kemudian didaftarkan dengan identitas persis, lease eksklusif dan penggantian metadata atomik. **Tidak ada restart ketiga, migrasi ulang, restore, penyalinan source ulang atau baseline pengganti.** Pemilihan child Python pada helper revisi diuji terpisah; seluruh body starter revisi tersebut tidak dijalankan. Peluncur produk sudah mengabaikan child yang bukan aplikasi.

Pemeriksaan akhir memakai receipt asli dan source yang sama, sebelum/sesudah18 request. Readiness membuat dan menghapus berkas probe sementara miliknya; berkas/direktori perusahaan tetap identik. Tidak ada login perusahaan, penulisan database bisnis atau panggilan provider. Receipt lokal: `.local/quality-20261009/marketing-identity-company-runtime-owned-registration-20261010-1.json`.

Aplikasi berjalan pada [modul OSEE lokal](http://127.0.0.1:8001/modules/), server57352, launcher56152, mulai `2026-10-09T19:27:08.7731868Z` (10 Oktober WIB). Ini preview lokal yang teruji, bukan deployment cloud/HTTPS.

## Pekerjaan berikutnya

FIN-03: uang muka pemasok sebelum tagihan, penerapan ke utang tanpa kas ganda, residual dan pengembalian dari mutasi bank nyata. Saldo awal, workflow pajak lanjutan, payroll, Operations, Direktur, integrasi live dan UAT staf tetap mempunyai pekerjaan sendiri.
