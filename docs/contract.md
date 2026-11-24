# Pipeline batch interface contract

Pipeline schedulers provide an ordered list of 1..1000 product batches and 2..10000 cumulative-volume meter readings ordered by strictly increasing kilometre position. Batch and meter IDs are unique. Volumes and positions are bounded nonnegative decimal strings represented as exact rational numbers. The first cumulative reading must equal total nominated batch volume; downstream cumulative readings may only fall.

Each adjacent meter pair reports observed loss and a permitted loss derived from loss_permille. The terminal cumulative volume is allocated to batches in nomination order, preserving delivered volume, shortfall and each completely crossed batch interface. The baseline assumes piston-like ordered flow and does not model mixing zones, density correction, withdrawals, reverse flow or time-varying rate.

The result is a structured reconciliation used by terminal accounting, not inventory CRUD, a generic report or a physical control system. A future mixing-quality module can attach chromatograph evidence to the stable upstream/downstream batch interface identities.

