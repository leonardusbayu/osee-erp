# Refund uang saldo lama — 11 Oktober 2026

**Status lingkup fitur: perilaku tersimpan lulus pada SQLite dan PostgreSQL. Penerimaan pengguna nyata masih NEEDS_EVIDENCE; seluruh ERP belum siap (all_erp_ready: false).**

## Yang bisa dilakukan

Finance mencatat uang yang benar-benar dikembalikan untuk piutang lama pelanggan/mitra atau uang yang benar-benar dikembalikan pemasok terhadap pembayaran utang lama. Pengembalian harus ditautkan ke mutasi bank yang berbeda dan berlawanan arah. Finance mengikuti **Finance → Piutang & utang lama → Lihat rincian → pembayaran asli → Cocokkan pengembalian uang → Periksa sumber → Konfirmasi dan simpan**.

Jumlah pengembalian dibatasi oleh pembayaran asal yang belum dikembalikan dan sisa mutasi bank. Pembayaran asli, bukti, jurnal, tanggal, dan pemeriksaan pajak awal tetap tersimpan. Pengembalian membuka kembali bagian pokok piutang/utang mulai tanggal refund, tanpa membuat penjualan atau biaya baru. Pemeriksaan pajak atas refund adalah tugas tersendiri; aplikasi meminta bukti profesional dan tidak menetapkan perlakuan pajaknya sendiri.

Role Finance dapat mencatat refund; Owner dapat mencatat dan mengelola koreksi terkait; peran baca hanya membaca; Marketing tidak mendapat akses. Isian diperiksa sebelum konfirmasi dan dipertahankan saat sumber berubah. Riwayat refund tampil terpisah dari pembatalan salah cocok.

## Bukti uji

Pada source post-format, suite SQLite menemukan 17 kasus: **15 lulus dan 2 dilewati** khusus karena memerlukan PostgreSQL. Suite finance pada PostgreSQL 16.14 lulus **8/8**, termasuk dua uji persaingan pembatalan/refund dan pemakaian mutasi bank ganda. Check sistem, migrasi dan lint fatal lulus. Database PostgreSQL QA sementara dihentikan dan dihapus.

Gate QA memberi NEEDS_EVIDENCE, karena belum ada UAT staf authenticated di perangkat nyata, aksesibilitas, validasi dokumen/pajak perusahaan, atau cloud/domain/HTTPS dan backup/restore. Lihat paket [SQLite refund QA](qa-runs/opening-cash-refunds-2026-10-11-01/report.md) dan [PostgreSQL refund QA](qa-runs/opening-cash-refunds-2026-10-11-02/report.md). Percobaan awal dengan Python global tanpa Django tersimpan sebagai bukti lingkungan; pengujian final memakai virtualenv proyek.

Pemindaian Aislop 0.18.1 pada 11 file refund menghasilkan 0 error dan 75 warning (skor 45/100, label Critical). Warnings Django Meta dan model re-export ditriase sebagai framework false positive; warning ukuran/fungsi/dispatch tetap merupakan utang maintainability. Rincian: [triage kualitas](qa-runs/opening-cash-refunds-2026-10-11-01/evidence/quality-triage.md).

## Batas operasional

Tidak ada data refund perusahaan yang dimasukkan atau dikoreksi oleh QA. Alur tidak mengirim uang. Keputusan PPh dan bukti fiskal wajib diperiksa oleh pihak profesional; sistem tidak menyatakan PT Langkah Pintar Nusantara patuh atau mengoptimalkan pajak. BNI tetap memakai impor statement sampai akses API resmi tersedia.
