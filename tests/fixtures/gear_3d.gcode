;PREVIEW:iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==
;VERSION:7.10.12
;PRINTING_MODE:[classic]
;MINX:-16.6
;MINY:-16.6
;MINZ:0.0
;MAXX:16.6
;MAXY:16.6
;MAXZ:10.1
;FLAVOR:RepRap
;TIME:1534
;Filament used: 0.93m
;Layer height: 0.15
;Generated with Cura_SteamEngine 3.6.0
T0
M140 S115
M105
M190 S115
M104 S290
M105
M109 S290
M82 ;absolute extrusion mode
;STE Slicer 5D Gcode
START
G1 F200 E15 ;extrude 15 mm of feed stock
G1 F200 E13.5 ;make retraction
G92 E0 ;zero the extruded length again
G54
G4 P100;Move to the first coordinate system
G92 E0
G92 E0
G1 F1500 E-1
;LAYER_COUNT:3
;LAYER:0
M107
G0 F3600 X159.2 Y135.6 Z0.3
;TYPE:WALL-OUTER
G1 F1500 E0
G1 F1800 X192.0 Y135.6 E1.2
G1 X192.0 Y164.7 E2.4
G1 X159.2 Y164.7 E3.6
G1 X159.2 Y135.6 E4.8
;LAYER:1
M106 S255
G0 X160.0 Y136.0 Z0.45
;TYPE:FILL
G1 X191.0 Y136.0 E6.0
G1 X191.0 Y164.0 E7.1
;LAYER:2
G0 X160.0 Y136.0 Z0.6
G1 X191.0 Y136.0 E8.2
;TIME_ELAPSED:1534.0
G1 F1500 E7.2
M107
M104 S0
M140 S0
;prefix_end_gcode
END
;SETTING_3 {"global_quality": "[general]\\nversion = 4\\nname = Normal #2\\ndefi
;SETTING_3 nition = fdmprinter\\n\\n[metadata]\\nsetting_version = 5\\ntype = qu
;SETTING_3 ality_changes\\nquality_type = fast\\n\\n[values]\\nprinting_mode = c
;SETTING_3 lassic\\nsupport_enable = False\\nadhesion_type = brim\\n\\n", "extru
;SETTING_3 der_quality": ["[general]\\nversion = 4\\nname = Normal #2\\ndefiniti
;SETTING_3 on = fdmprinter\\n\\n[metadata]\\nsetting_version = 5\\ntype = qualit
;SETTING_3 y_changes\\nquality_type = fast\\nposition = 0\\n\\n[values]\\ninfill
;SETTING_3 _sparse_density = 100\\nwall_line_count = 4\\nmaterial_print_temperat
;SETTING_3 ure = 290\\n\\n", "[general]\\nversion = 4\\nname = Normal #2\\ndefin
;SETTING_3 ition = fdmprinter\\n\\n[metadata]\\nsetting_version = 5\\ntype = qua
;SETTING_3 lity_changes\\nquality_type = fast\\nposition = 1\\n\\n[values]\\n\\n
;SETTING_3 "]}
