# Pembatalan pencocokan dan refund saldo lama — rencana implementasi

Status awal **PLANNED / NEEDS_EVIDENCE**. Baseline terpasang dan terverifikasi adalah `a42fae989bd3d21507a5e044cdd9f936e68ddf65543fa7a5b24f1e1dd20ba850`; snapshot 593 file publik tersimpan pada bukti privat batch ini. Tidak ada penerimaan fitur pemulihan kas atau kesiapan seluruh ERP: `all_erp_ready: false`.

Tujuan tetap seluruh ERP yang fungsional, minim bug, dan mudah dipakai staf nonteknis. Increment ini melanjutkan [status terakhir](FULL-APP-STATUS-CORRECTIONS-2026-10-10.md), bukan mengganti tujuan tersebut dengan pembatalan saja. Cabang yang belum tersedia tetap harus diselesaikan dan diverifikasi sebelum tujuan keseluruhan selesai.

## Dua kejadian yang berbeda

**Batalkan pencocokan** memperbaiki hubungan mutasi bank yang salah. Owner membatalkan seluruh pencocokan asal dengan alasan dan bukti. Mutasi fisik, posting asli, dokumen, jumlah, jurnal asli, dan audit tetap tersimpan. Keputusan pembatalan mempunyai jurnal pembalik sendiri serta mengembalikan kapasitas bersama mutasi menjadi belum dicocokkan. Tidak mengirim atau mengembalikan uang nyata.

Untuk cabang pertama, tanggal efektif pembatalan harus **sama dengan tanggal bank/pencocokan asli**, ditampilkan tetap; waktu pencatatan audit adalah waktu tindakan. Periode sumber harus terbuka. Pembatalan bertanggal lebih kemudian tidak boleh melepas kapasitas yang kemudian dipakai lagi untuk mencatat kas pada tanggal lebih awal. Pembetulan setelah buku tertutup memerlukan alur amendemen tersendiri; tanggal tidak boleh digeser untuk melewati proteksi.

**Cocokkan pengembalian uang** memerlukan mutasi bank lain yang benar-benar terjadi, dengan arah berlawanan: uang keluar untuk refund penerimaan AR, uang masuk untuk pengembalian pembayaran AP. Jumlah positif dan tanggal asli bank, paling awal tanggal sumber, harus berada dalam kapasitas mutasi serta sisa pembayaran asal yang belum dikembalikan. Refund menambah kejadian, posting, jurnal, dan bukti sendiri; tidak mengubah pembayaran asal atau menganggapnya tidak pernah terjadi.

Refund saldo lama membuka kembali bagian piutang/utang yang dikembalikan. Jika dokumen asal juga berubah, koreksi pokok memerlukan dokumen dan alur Owner tersendiri. Tidak otomatis membuat invoice, biaya, pendapatan, bruto kontrak, pemotongan atau kredit pajak.

## Batas dan public seams

Pengujian meneruskan batas publik FIN04 yang sudah dipakai: command/preview sumber, posisi bertanggal, pembukuan/kapasitas bank bersama, HTTP staf, dan dependensi laporan pajak. Tidak menguji helper internal sebagai kebenaran saldo.

- `cancel_opening_settlement(organization, actor, settlement, reason, evidence, request_key, expected_fingerprint, confirmed)`: jumlah penuh dan tanggal berasal dari sumber, tanpa override jumlah/tanggal.
- `opening_balance_action_context(action="cancel", settlement=...)`: tampilkan sumber, jumlah dilepas, saldo sebelum/sesudah, kapasitas bank sebelum/sesudah, dan alasan penolakan.
- `refund_opening_settlement(...)` dan preview `action="refund"`: tautan pembayaran asal, mutasi pengembalian, jumlah positif, bukti, UUID, fingerprint dan konfirmasi.
- Public posisi, close facts, receipt snapshots/graphs, report balances, pending-bank query, monthly/annual dependencies, dan HTTP tetap menjadi batas baca ulang.

Setiap command memakai lock organisasi terlebih dahulu, peran sebelum replay/seleksi, scope semua FK, source proof lengkap, konfirmasi, dan replay identik sebelum batas buku yang berubah. Payload berbeda dengan kunci sama ditolak. Kesalahan audit membatalkan seluruh transaksi. Original receipt/proof/schema tetap kompatibel; recovery valid tidak mengubah identitas sumber kas asal.

Pembatalan penuh pertama memerlukan sumber utuh yang belum mempunyai refund atau otoritas pemeriksaan profesional diterima. Cabang pelepasan otoritas profesional harus dirancang terpisah dengan bukti penerimaan/revisi; menolak sementara bukan berarti gap itu selesai. Pembatalan sebagian, pembatalan refund keliru, dan amendemen periode tetap tercatat sebagai pekerjaan lanjutan.

## Pembukuan dan contoh independen

Dengan bank awal 1.000, AR 300 dan AP 200, penerimaan AR 120 serta pembayaran AP 50 menghasilkan BANK 1.070, AR 180, AP −150, EQUITY −1.100. Pembatalan salah pencocokan AR penuh pada tanggal bank membuat BANK 950 dan AR 300, dengan AP/ekuitas tetap. Mutasi bank asal 120 menjadi belum dicocokkan. Pencocokan ulang kas yang sama menghasilkan kembali BANK 1.070 dan AR 180, tanpa mutasi bank fisik baru.

Pembatalan AR: Dr AR / Cr BANK; AP: Dr BANK / Cr AP. Refund AR: Dr AR / Cr BANK pada mutasi keluar baru; refund AP: Dr BANK / Cr AP pada mutasi masuk baru. Seluruh prefix tanggal, kapasitas bersama dan sisa setiap pembayaran harus konservatif, bukan hanya saldo akhir. Sumber rusak/foreign/orphan membuat jumlah belum dapat dinilai dan token pemeriksaan berubah saat diperbaiki.

## Pemeriksaan pajak dan riwayat laporan

Pembatalan harus memeriksa sumber pokok dan kas, otoritas profesional diterima, persetujuan/revisi bulanan yang eksplisit, kewajiban berlanjut, paket tahunan disetujui/dilaporkan, dan buku sumber tertutup. Tidak melepas persetujuan atau menarik kewajiban otomatis. Revisi draf saja tidak cukup.

Refund mempunyai bukti dan tugas pemeriksaan fiskal sendiri. Laporan asli tanpa pemotongan tidak menjadi keputusan refund. AR refund tidak otomatis mengurangi omzet; AP refund tidak otomatis menjadi pendapatan. Riwayat fiskal harus menghubungkan kas asal, recovery dan pencocokan ulang agar mutasi yang sama tidak mendapat alokasi fiskal ganda. Kas yang dibatalkan tetap mempunyai bukti/lineage; penghapusan kandidat atau nilai pajak nol tidak boleh dipakai sebagai pengganti keputusan yang belum tersedia.

Recovery kemudian harus masuk inventory komersial bertanggal dari tanggal efektifnya. Identitas receipt/otoritas asli tetap terpisah sehingga laporan sebelum tanggal recovery yang valid tidak menjadi stale. Sumber recovery rusak harus tetap terdeteksi lewat anchor asli dan current, termasuk metadata keputusan, jurnal/baris, sumber bank, serta bukti PDF jika ada.

## UI dan penerimaan

Tindakan berada pada baris pembayaran asli, termasuk saldo yang sudah lunas. Owner membatalkan; Owner/Finance mencocokkan refund; pembaca hanya membaca dan Marketing ditolak. Alur **isi → Periksa sumber → Konfirmasi dan simpan** tetap formulir server tanpa JavaScript, mempertahankan isian ketika stale dan meminta pemeriksaan/konfirmasi baru. Tampilan historis tidak menawarkan tindakan. Label riwayat: **Pencocokan dibatalkan** / **Uang dikembalikan**, dengan pencatat dan dokumen.

Root merekam RED sebelum implementasi, mempertahankan tracer/metode asli, lalu GREEN per slice. Gate berikutnya: konservasi AR/AP/shared bank, replay/role/scope/CSRF, stale retention, tanggal tertutup, otoritas fiskal, corruption/orphan, PostgreSQL race, full SQLite, quality dan browser desktop/ponsel, kemudian backup dan pemasangan tanpa mengubah transaksi perusahaan. Tidak menjalankan provider atau memakai data perusahaan pada QA. Git tidak diubah oleh batch ini.

Penerimaan hanya boleh menyatakan cabang yang terbukti. Reviewed cancellation, refund-specific fiscal review/history projection, partial/correction cases, UAT staf, sumber perusahaan, BNI API resmi dan cloud/domain/HTTPS tetap berada dalam tujuan keseluruhan hingga bukti selesai.
