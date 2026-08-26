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
calib25 | dispense 10ul, high flow, 384 plate, 1ul cassette required, odd rows only
calib26 | dispense 10ul, high flow, 384 plate, 1ul cassette required, not all rows, 19 steps (0.87) right of center (one step = 0.05mm)
calib27 | dispense 10ul, high flow, 384 plate, 1ul cassette required, not all rows, 19 steps (0.87) left of center
calib28 | dispense 10ul, high flow, 384 plate, 1ul cassette required, not all rows, 19 steps (0.87) left of center, 6 steps (0.44mm) front of center
calib29 | dispense 10ul, high flow, 384 plate, 1ul cassette required, not all rows, 19 steps (0.87) left of center, 6 steps (0.44mm) back of center
calib30 |

Integration note: calib26-calib29 prove the signed horizontal position fields.
Positive X moves right and negative X moves left; positive Y moves forward and
negative Y moves back. The driver exposes these as `x_offset_steps` (-60..60)
and `y_offset_steps` (-40..40) for every peristaltic dispense, independent of
cassette type. Both default to zero. The limits come from the operator manual.
These offset fixtures were integrated offline and were not run on hardware.
