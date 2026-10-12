# Dependensi migrasi Marketing

Status 10 Oktober: INF-01 selesai pada ad9 dengan gate scoped ketiga diterima dan pemeriksaan runtime/data asli lulus. Kegagalan gate pertama/kedua tetap tersimpan. Rincian cakupan dan pengujian yang digunakan ulang berada pada [laporan perbaikan](MARKETING-MIGRATION-DEPENDENCY-RELEASE-2026-10-10.md). Kontrak dan reproduksi awal di bawah tetap menjadi riwayat penerimaan.

INF-01 menangani instalasi baru yang menargetkan modul Marketing. Pada source `e32c5d84…`, dua perintah nyata pada database SQLite kosong terpisah, menuju Marketing0008 dan Marketing0009, gagal di Marketing0005: tabel model `director.Decision` belum tersedia. Kegagalan serta cleanup normal tercatat pada `marketing-dependency-cold-red-20261009-1`; source belum diubah saat reproduksi. Migrasi penuh sebelumnya lulus karena modul Direktur ikut diterapkan lebih dahulu.

Perbaikan terbatas menambahkan dependensi Director0001 ke Marketing0005. Seluruh operasi migrasi, model, aturan bisnis dan UI tetap sama. Database perusahaan sudah mencatat Director0001 sebelum Marketing0005; perbaikan harus membuktikan histori itu masih konsisten dan tidak menjadwalkan migrasi atau menulis ulang catatan perusahaan.

Penerimaan membutuhkan replay kedua perintah SQLite asli tanpa mengganti oracle, dua perintah setara pada database baru PostgreSQL QA, pemeriksaan graph tanpa cycle, serta tes permanen yang membangun state historis Marketing dan menyelesaikan relasi Decision/Budget. Pemeriksaan kualitas dan regresi migrasi terkait harus lulus. Provider dimatikan; database perusahaan dan dokumen asli bukan fixture.

Bukti native sebelum/sesudah harus menunjuk source yang sama. Cluster/database QA yang baru dialokasikan dibersihkan normal hanya setelah identitas dan sesi diperiksa. Sumber e32 serta gate MKT-03 yang sudah diterima tetap menjadi checkpoint historis; hasil INF-01 dipublikasikan terpisah. Seluruh backlog ERP, UAT staf dan akses live tetap terbuka.
