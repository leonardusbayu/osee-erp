# Koreksi sumber iklan marketing

Pembaruan 10 Oktober: temuan INF-01 yang masih terbuka pada checkpoint MKT-03 kemudian diselesaikan dalam [batch dependensi terpisah](MARKETING-MIGRATION-DEPENDENCY-RELEASE-2026-10-10.md). Penerimaan historis e32 dan kegagalan awal tetap disimpan; tidak ada perluasan gate MKT-03.

Status: MKT-03 selesai, diuji dan diterapkan lokal pada source `e32c5d84…`. Gate ketiga `READY_FOR_SCOPE` mencakup penarikan dan penggantian satu baris sumber iklan. SQLite: 924 kasus, 882 lulus dan 42 khusus PostgreSQL dilewati; PostgreSQL: seluruh 924 lulus dengan cleanup normal. Pemeriksaan independen 7 kasus, browser 4 alur/78 keadaan desktop dan ponsel, serta pemeriksaan skema/kode/dependensi lulus. Migrasi mempertahankan 975 catatan asli pada 123 tabel dan seluruh dokumen; tambahan hanya lima definisi framework dan satu catatan migrasi, tanpa transaksi atau keputusan pemulihan baru. Lima belas pemeriksaan setelah startup lulus pada `http://127.0.0.1:8001`. Lihat [laporan rilis terbatas](MARKETING-AD-RECOVERY-RELEASE-2026-10-09.md). Dokumen ini tidak menyatakan seluruh aplikasi siap.

Riwayat penerimaan tetap disimpan. Dua reproduksi publik pada source `3bb3bda4…` gagal karena form penarikan/penggantian belum ada. Pada build `65505455…`, seluruh 923 assertion PostgreSQL lulus tetapi perintah gagal saat membersihkan database tes; gate pertama tetap `NEEDS_EVIDENCE`. Probe dua tes pajak lama membuktikan koneksi worker sehat masih terbuka. Cleanup diperbaiki tanpa mengubah aturan pajak atau assertion bisnis. Kegagalan asli dan kesalahan tipe fixture migrasi tetap disimpan, tanpa melonggarkan perbandingan nilai.

## Hasil untuk staf

Penerimaan kedua pada `d6151585…` berhenti setelah satu kegagalan fixture antrean harian: tes menjadwalkan dua jam dari sekarang tetapi selalu mengharapkan jadwal hari ini. Reproduksi dengan method asli lulus pada 10.00 WIB dan gagal pada 23.30 WIB. Gate kedua tetap `NOT_READY`; browser dan suite PostgreSQL kedua tidak dijalankan. Jam fixture diperjelas tanpa mengubah assertion bisnis, dan tes tambahan memeriksa jadwal UTC yang melintasi tengah malam WIB. Seluruh pemeriksaan penerimaan ketiga dijalankan ulang pada source yang sama sebelum migrasi perusahaan.

Staf dapat menarik data iklan yang keliru atau menggantinya setelah salah memilih kampanye, tanggal, iklan, audiens, wilayah atau tingkat rincian. Halaman menunjukkan data lama dan data pengganti sebelum simpan, meminta alasan/bukti, lalu memperlihatkan hasil dan riwayat. Kolom angka yang sudah mempunyai koreksi tetap memakai alur yang didukung.

Hanya versi aktif berkontribusi ke laporan dan KPI. Penarikan data Rp100 tidak berarti biaya sebenarnya nol: kelengkapan sumber perlu ditinjau kembali. Data yang pernah disimpan, review KPI, bukti impor, pembuat dan keputusan lama tetap dapat dibaca.

## Celah sebelum implementasi

`AdDailyObservation` menjaga identitas sumber dan keunikan grain/reference, tetapi belum mempunyai penarikan/pengganti untuk identitas yang salah. Form koreksi mengunci kampanye/tanggal/grain/reference. Impor CSV sudah atomik, memiliki replay, dan koreksi angka sudah diaudit. Pertahankan kemampuan tersebut.

Pembaca analytics, kelengkapan sumber, KPI, penyelesaian kampanye, ringkasan manajemen dan konfirmasi rekomendasi saat ini membaca semua observation. Satu proyeksi sumber aktif harus menjadi acuan bersama; jalur audit/impor tetap membaca seluruh riwayat. Register lama `director.MarketingObservation` tetap dibedakan dari sumber iklan marketing ini.

## Kontrak mutasi dan identitas

- Sediakan penarikan dan penggantian atomik dengan organisasi, actor aktif, observation terpilih, versi/fingerprint yang dilihat, request unik, alasan dan bukti. Pertahankan peran penulis yang sudah diizinkan; Auditor hanya membaca.
- Simpan identitas dan payload asli, pembuat, tanggal serta riwayat penarikan/pengganti secara immutable. Satu pendahulu hanya mempunyai satu pengganti. Retry identik menghasilkan hasil yang sama; isi berbeda atau tab kedaluwarsa ditolak dengan masukan tetap terlihat.
- Reference dokumen tetap milik rantai riwayatnya. Staf tidak diminta membuat reference dokumen palsu untuk memperbaiki data. Request koreksi berbeda dari identitas dokumen asli; detail teknis disimpan server.
- Penggantian pada batch ini menerima satu baris sumber nyata untuk satu baris lama. Perubahan total menjadi satu rincian tidak menyatakan seluruh rincian sudah lengkap: rincian lain dari dokumen nyata dapat ditambahkan melalui impor CSV atomik yang tersedia, lalu cakupan sumber diperiksa ulang. Penggantian satu total menjadi beberapa baris dalam satu keputusan belum termasuk batch ini.
- Keunikan dan larangan overlap total/rincian diperiksa terhadap sumber aktif. Validasi kampanye, tanggal, scope dan kapasitas terjadi sebelum transaksi penggantian diterima. Jika pengganti bertentangan, sumber lama tetap aktif dan tidak ada setengah perubahan.
- Kunci Organization diperoleh dahulu. Perlombaan dua pengganti atau tarik-versus-ganti hanya menerima satu hasil yang konsisten. Tidak ada jurnal, pembayaran atau kas Finance yang dibuat/diganti.

## Proyeksi dan review

Gunakan satu pembaca organisasi/periode untuk identitas dan nilai aktif. Biaya, impressions, clicks, rasio dan jumlah baris harus berasal dari proyeksi yang sama. Penarikan atau penggantian bernilai sama tetap mengubah provenance, kelengkapan sumber, KPI dan identitas advice.

Fingerprint mengikat fakta aktif serta keputusan pemulihan yang mengenai scope lama atau baru. Membuat lalu menarik sumber hingga kumpulan aktif kembali kosong tetap membatalkan pemeriksaan lama; hasil kosong tidak menyamarkan perubahan riwayat. Keputusan dan bukti tetap lokal, tanpa memperluas payload privat yang dikirim ke AI.

Review/snapshot lama tetap tersimpan dengan nilai lama; sistem menandainya sudah perlu ditinjau kembali. Konfirmasi rekomendasi dengan sumber lama tidak boleh dianggap review data pengganti. Propagasi ke Direktur/API/CSV/AI memakai snapshot manajemen yang sama. Kelengkapan sumber tidak boleh mengaku lengkap ketika evaluasi masih belum lengkap.

Impor ulang dokumen lama tidak mengaktifkan sumber yang sudah ditarik, tidak menambah versi pengganti, dan tidak menghapus keputusan koreksi. Halaman detail baca-saja menyediakan drilldown untuk Auditor tanpa mengarahkannya ke form tulis.

## Penerimaan

1. Tarik satu-satunya sumber Rp100: sumber aktif tidak tersedia, kelengkapan/KPI perlu review, riwayat Rp100 tetap terbaca.
2. Ganti kampanye/tanggal yang salah: hanya pengganti dihitung dan kedua scope yang terdampak perlu review; jurnal/kas Finance tidak berubah.
3. Ganti total dengan rincian: jumlah benar, tanpa overlap. Pengganti yang berbenturan ditolak seluruhnya dan sumber lama tetap aktif.
4. Ganti dengan nominal sama: provenance/advice berubah dan review/konfirmasi lama ditolak sebagai kedaluwarsa.
5. Nilai belum diketahui berbeda dari nol yang berbukti; menarik sumber bernilai nol tidak mengesahkan nol baru.
6. CSV lama tidak menghidupkan kembali sumber yang sudah ditarik atau menggandakan pengganti.
7. Retry identik serta race PostgreSQL nyata menerima satu hasil; konflik payload/pengganti/penarikan tetap dapat dijelaskan kepada staf.
8. Data salah tanggal yang ditarik berhenti menghalangi penyelesaian kampanye; data aktif yang masih bertentangan tetap menjadi blocker.
9. Batas organisasi/peran, pengguna tidak aktif, CSRF, versi dan source fingerprint ditegakkan server. Form gagal mempertahankan masukan; desktop/ponsel memverifikasi data tersimpan, bukan hanya pesan sukses.

Reproduksi asli diputar ulang tanpa mengganti ekspektasi; pengujian independen, seluruh suite SQLite/PostgreSQL, browser dan gate ketiga lulus. Persiapan migrasi awal gagal di database referensi terpisah karena dependensi Director0001 tidak dinyatakan oleh Marketing0005. Kegagalan terjadi sebelum migrasi perusahaan. Kelanjutan terbatas memakai backup/baseline asli yang sama, membangun referensi dari tepat 60 migrasi historis, membandingkan DDL lama/baru, lalu menerapkan hanya Marketing0009. Dependensi source tersebut masih menjadi pekerjaan INF-01, bukan dianggap diperbaiki oleh kelanjutan referensi. Adapter/live Meta/Google, izin akun, KPI tambahan, penggantian satu total menjadi beberapa baris, serta UAT staf tetap terpisah.
