# Rilis lokal Operations: kursus multi-pertemuan

Tanggal: 11 Oktober 2026
Cakupan: pencatatan jadwal, kehadiran, dan progres kursus; implementasi lokal

## Perubahan

Finance dapat menetapkan jumlah pertemuan sesuai penawaran yang sudah dikonfirmasi, menjadwalkan setiap pertemuan, mengubah jadwal sebelum kelas dimulai, dan mencatat hadir/tidak hadir per peserta dengan referensi bukti. Koreksi membutuhkan alasan dan persetujuan. Riwayat menampilkan pengguna, waktu, status, alasan, bukti lama, dan bukti pengganti.

Halaman peserta kursus menampilkan progres seluruh paket dan mengarahkan staf ke jadwal kursus. Aksi satu sesi untuk tes tidak ditampilkan atau diterima untuk produk kursus. Pembatalan setelah peserta mengikuti pertemuan ditahan agar kredit satu paket tidak otomatis diberikan.

Penutupan Finance mengenali jadwal kursus mendatang, kehadiran yang belum lengkap, dan sesi yang sudah dilaksanakan. Jika suatu sesi sudah dihadiri, penutupan bulan menunggu kebijakan pengakuan pendapatan kursus yang disetujui dan penerapannya. Fitur ini tidak membuat jurnal atau mengarang pengakuan pendapatan.

## Pemeriksaan

Tes fitur kursus lulus 13/13 setelah pemecahan alur tes dan halaman khusus kursus. Suite Operations bersama tes pemilih topik pajak lulus 39 tes, 3 dilewati, dan tidak gagal. Regresi penuh SQLite sebelumnya lulus 1.292 tes dengan 69 dilewati; regresi penuh tidak diulang setelah pemisahan modul tampilan. Pemeriksaan Django, Ruff terarah, dan `makemigrations --check --dry-run` lulus. Rincian: [laporan QA](qa-runs/course-meetings-2026-10-11-01/report.md).

## Batas rilis

Ini perubahan lokal; belum dipasang pada server cloud. Finance belum memvalidasi kontrak kursus dan kebijakan akuntansi pengakuan pendapatan. Jadwal pengganti untuk peserta yang tidak hadir dan perlakuan keuangan saat pembatalan setelah sebagian layanan memerlukan kebijakan usaha/akuntansi sebelum dibuat otomatis. UAT staf dan penerimaan lintas-modul tetap terbuka; ERP keseluruhan belum siap dipakai operasional.
