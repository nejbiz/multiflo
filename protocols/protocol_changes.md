calib2 | dispense 200 ul, medium, all columns (1111111111), any cassette, DW, predispense 2x10ul, rows (1)
calib3 | dispense 200 ul, medium,only first column, any cassette, DW
calib4 | dispense 200 ul, medium,only first column, any cassette, standard 96 well plate
calib5 | dispense 200 ul, hihg flow,only first column, any cassette, standard 96 well plate
calib6 | dispense 1000 ul, high flow,all columns, any cassette, standard 96 well plate
calib7 | dispense 1000 ul, low  flow,all columns, any cassette, standard 96 well plate
calib8 | prime, 3000 ul, medium flow, any casette - this is the max allowed volume per prime step
calib9 | purge, 2000 ul, medium flow, any casette
calib10 | shake 5s, move carrier home first, medium speed
calib11 | soak 30s, move carrier home first, medium speed
calib12 | dispense 200 ul, loop over 3 times, deep well plate 44.58 mm (975) above carrier (Z offsett)
calib13 | delay fixed duration 5 min, sound at begining, deep well plate
calib14 | delay indefinite duration, no sound at begining, deep well plate
calib15 | shake 30 s medium, do not move carrier to home
calib16 | shake 45 s medium, move carrier to home
calib17 | dispense 750 ul, medium flow, 4x 10 ul predispense
calib18 | dispense 750 ul, medium flow, 4x 50ul predispense
calib19 | dispense 750 ul, medium flow, no predispense
calib20 | dispense 750 ul, high flow, required 5 uL cassette (cassette type changed on machine interface)
calib21 | dispense 750 ul, high flow, required 1 uL cassette
calib22 | dispense 750 ul, high flow, required 10 uL cassette
calib23 | dispense 1 ul, low  flow,  384 well plate, required 1 uL cassette
calib24 | prime 3000ul, hihg flow, 384 plat, 1ul required cassette

Integration note: calib21 is retained as evidence but rejected by the driver.
Its 750 uL dispense exceeds the operator-manual 1-50 uL range for a required
1 uL cassette. The cassette requirement fields in calib20-calib24 are treated
as requirements to verify, never as authorization to change the onboard setting.
