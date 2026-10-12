# SDM & Penggajian

## Alur kerja

Finance menambahkan karyawan atau tutor/kontraktor, menyiapkan satu draf untuk satu bulan, memasukkan rincian bruto dan nominal PPh 21/BPJS yang sudah diperiksa, lalu mengajukan payroll. Owner/Direktur yang berbeda dari pembuat pengajuan memeriksa dan menyetujui atau menolak dengan alasan. Persetujuan membuat jurnal payroll. Finance kemudian mencocokkan transfer yang benar-benar terlihat pada mutasi bank.

Rincian draf masih dapat diperbaiki atau dihapus dengan alasan yang tercatat. Setelah diajukan, rinciannya terkunci. Jika ditolak, Finance membuat revisi baru. Payroll terposting yang belum memiliki pembayaran bank dapat dikoreksi dengan jurnal pembalik dan revisi baru. Payroll yang sudah memiliki pembayaran bank tidak dapat dibalik dari layar ini; koreksi tersebut perlu ditangani melalui alur akuntansi bank yang sesuai sebelum payroll dikoreksi.

## Batas perhitungan dan pencatatan

Aplikasi **tidak menghitung** PPh 21 atau BPJS dan tidak menetapkan perlakuan pajak untuk tutor/kontraktor. Finance memasukkan nominal yang sudah diperiksa dan mencatat rujukan dokumennya. Owner/Direktur meninjau rincian tersebut sebelum jurnal dibuat. Jangan menggunakan angka perkiraan.

Rumus pencatatan payroll yang dimasukkan adalah:

- Bersih dibayar = bruto − PPh 21 yang dicatat − BPJS bagian karyawan.
- Biaya perusahaan = bruto + BPJS bagian perusahaan.
- PPh 21 masuk ke akun kewajiban pajak; kewajiban gaji dan BPJS masuk ke utang usaha. Pencocokan mutasi aktual mengurangi kewajiban tersebut.

Angka PPh 21 di modul ini belum menjadi laporan pajak masa, bukti potong, atau SPT. Fitur pelaporan pajak tetap mengikuti alur pajak yang terpisah dan memerlukan validasi data serta aturan pajak perusahaan.

## Hak akses dan privasi

- Finance dapat melihat dan menyiapkan payroll, mengelola daftar orang, dan mencocokkan mutasi bank.
- Owner/Direktur dapat melihat, menyetujui, menolak, serta mengoreksi jurnal payroll yang belum dibayar.
- Marketing tidak dapat membuka modul atau melihat daftar orang dan nominal.
- Modul tidak meminta NIK, alamat rumah, atau nomor rekening pribadi. Jangan menambahkan data pribadi yang tidak diperlukan ke catatan atau rujukan.
- Ringkasan payroll hanya masuk ke database perusahaan. Modul tidak mengirim nama atau nominal payroll ke AI.

## Pemeriksaan implementasi

Uji workflow memakai database sementara dan data sintetis. Terapkan migrasi `finance.0018` dan `people.0001`–`people.0002` pada lingkungan aplikasi melalui prosedur deployment yang disetujui sebelum staf menggunakan modul. Jangan gunakan database lokal perusahaan sebagai database uji.

## Verifikasi terbatas — 10 Oktober 2026

Suite SDM/penggajian lulus **14 tes** pada SQLite in-memory. Cakupan meliputi akses Finance/Owner/Direktur dan penolakan Marketing, isolasi organisasi, input nominal manual dengan rujukan, persetujuan berbeda dari pengaju, jurnal seimbang, revisi dan riwayat, serta pencocokan mutasi bank. Ruff terpilih, pemeriksaan sistem Django, dan pemeriksaan migrasi lulus.

Tes pertama untuk peringatan privasi pada catatan bebas gagal karena petunjuk belum tampil; helper text ditambahkan dan seluruh 14 tes kemudian lulus. Bukti terinci, backup/migrasi, dan gate yang dapat direplay ada pada [paket QA](qa-runs/people-payroll-2026-10-10-05/report-final.md).

Gate untuk aktivasi lokal memberi `NEEDS_EVIDENCE`: server lokal kini berjalan, tetapi pemeriksaan browser terautentikasi, visual/responsif, UAT staf, dan PostgreSQL belum selesai. Sebelum migrasi, database 138 tabel dibackup dan lolos pemeriksaan integritas. Finance `0018`, Marketing `0011`, dan People `0001`/`0002` diterapkan; seluruh jumlah baris bisnis lama tetap sama, empat tabel payroll baru kosong, dan metadata framework bertambah sesuai empat model baru. Penerapan cloud masih memerlukan prosedur migrasi, backup, dan UAT tersendiri. Aislop 0.18.1 melaporkan 0 error dan 22 peringatan style/maintainability di direktori People; Ruff terpilih lulus.

## Penyederhanaan tindakan draf — 11 Oktober 2026

Setiap rincian payroll draf sebelumnya menampilkan tindakan ubah dan hapus dua kali: di tabel angka dan di daftar tindakan. Tabel kini berisi angka saja; ubah dan hapus tersedia satu kali pada daftar “Perbaiki rincian draf”. Alasan penghapusan tetap wajib. Tes halaman mereproduksi dua tautan/formulir sebelum perubahan dan lulus sesudahnya; 30 tes People/role lulus, bersama pemeriksaan Django dan migrasi. Bukti replay: [QA tindakan draf](qa-runs/people-payroll-ux-2026-10-11-02/report.md).

Gate ini `READY_FOR_SCOPE` hanya untuk penempatan tindakan draf dan regresi payroll sintetis. Browser staf terautentikasi, tampilan ponsel, UAT, PostgreSQL setelah perubahan, data gaji/pajak/BPJS nyata, dan cloud tetap belum diverifikasi. Ruff dari akar repositori lulus; Aislop 0.18.1 hanya menandai 11 urutan impor karena saat pemindaian direktori `people` ia menganggap `finance` sebagai paket eksternal. Tidak ada temuan Aislop untuk AI-slop, code-quality, atau security.

## Bahasa layar payroll — 11 Oktober 2026

Istilah pada layar diganti dengan kalimat yang lebih mudah dipahami staf nonteknis: “bruto” menjadi “sebelum potongan”, bukti nominal diberi label langsung, dan tombol persetujuan/koreksi menjelaskan tindakannya. Uji 16 People lulus pada database sementara, Django check dan Ruff lulus, serta Aislop 0.18.1 menemukan nol peringatan pada 17 file SDM. Bukti: [QA bahasa payroll](qa-runs/people-payroll-language-2026-10-11-01/report.md). Ini belum menggantikan UAT, validasi PPh 21/BPJS, atau pemeriksaan pada PostgreSQL dan cloud.
