# Sample deliverable text for testing SpecPay

SpecPay's AI reviews a **written description** of the delivered work. The text below describes a hypothetical bracket so you can try the review without doing any CAD work. It is sample text for the default brief on the home page, not a real file.

The acceptance criteria are written by the AI each time you scope a project, so they can differ slightly from run to run. If a review lists a missing item, add one sentence that answers it, in the same words as the criterion, and submit again. That is the intended retry loop.

## 1. A weak submission (should fail)

```
I made the bracket model. It has four holes and a flange. It should fit the relay box.
```

## 2. Milestone: 3D CAD model (should pass)

```
Delivered: solid 3D CAD model of an L-shaped sheet-metal mounting bracket for a 12 V relay box.

Material and thickness: 5052-H32 aluminium, 1.5 mm uniform thickness.

Overall dimensions: base plate 100 mm x 60 mm; one 90-degree flange, 35 mm high (outside), along a 100 mm edge. Inside bend radius 1.5 mm. Overall envelope 100 x 60 x 35 mm.

Mounting holes: exactly four through holes, diameter 5.5 mm (M5 clearance), all in the base plate. Hole centres measured from the front-left outside corner of the base plate: (10, 12), (90, 12), (10, 48) and (90, 48) mm. Pattern is 80 mm x 36 mm and symmetric about the plate centreline. Edge distance is 10 mm to the short edges and 12 mm to the front and rear edges. Hole edges are more than 4.5 mm from the bend tangent line, so bending does not distort them.

Fit: a relay box up to 70 x 50 x 35 mm sits centred on the base plate against the flange, with about 15 mm clearance to the hole centres on each side.

File format and compatibility: the model is a native sheet-metal part exported as a STEP AP214 file (relay_bracket_revA.step). The STEP file opens without errors in both SolidWorks and Autodesk Inventor, and the bend, the 1.5 mm thickness and all four 5.5 mm holes are preserved as editable features, so the model is fully compatible with SolidWorks and Inventor.
```

## 3. Milestone: 2D manufacturing drawings (should pass)

```
Delivered: 2D manufacturing drawing set for the sheet-metal relay bracket, A3 sheet, third-angle projection, scale 1:1.

Views: front, top and side orthographic views, an isometric view, and a flat-pattern view ready for laser cutting.

Dimensions labelled: overall 100 x 60 x 35 mm; plate thickness 1.5 mm; flange height 35 mm; inside bend radius 1.5 mm. Flat pattern 100 mm x 92.1 mm with the bend line located at 57 mm from the front edge.

Holes: four holes, diameter 5.5 mm, dimensioned by position from the front-left corner: (10, 12), (90, 12), (10, 48), (90, 48) mm. The hole callout reads "4X dia 5.5 THRU". Hole spacing 80 mm x 36 mm shown, plus edge distances 10 mm and 12 mm.

Tolerances stated on all mounting holes: diameter +0.1 / -0.0 mm; hole position +/-0.1 mm. General laser-cut tolerance +/-0.1 mm; bend angle +/-0.5 degrees; bend position +/-0.3 mm.

Laser cut line widths: the cut contour and hole outlines are drawn with a 0.25 mm continuous thick line, bend lines are 0.18 mm dashed lines, and dimension and centre lines are 0.13 mm thin lines, as shown in the line-width legend on the sheet.

File formats: the drawings are saved as a PDF (relay_bracket_revA.pdf) and as a DXF of the flat pattern (relay_bracket_revA.dxf).

Title block: part name, material 5052-H32 aluminium 1.5 mm, finish, quantity 1, revision A, date, units in millimetres. Notes: deburr all edges, break sharp edges 0.2 mm, bend direction up, grain direction not critical.
```

## 4. Milestone: final deliverables package (should pass)

```
Delivered package: all files are packaged in one zip archive named Bracket_Package_RevA.zip. All source CAD files, the exported STEP, PDF and DXF files are included in the zip archive.

Contents of the zip archive:
1. Source CAD files: the native sheet-metal part Bracket_3D.SLDPRT and the drawing source Bracket_Drawings.SLDDRW.
2. Exported STEP file: Bracket_3D.step (STEP AP214, bend defined).
3. PDF drawing set: Bracket_Drawings.pdf (orthographic views, isometric view and flat pattern, with dimensions and tolerances on all four mounting holes).
4. DXF of the flat pattern: Bracket_Drawings.dxf (100 x 92.1 mm, four dia 5.5 mm holes) for the laser-cutting shop.
5. Bill of materials listing: 1 x bracket, 5052-H32 aluminium, 1.5 mm thick, deburred, clear anodised (Type II); 4 x M5 x 12 mm pan-head screws and 4 x M5 washers for mounting.

File naming follows the required convention: Bracket_3D.step, Bracket_Drawings.pdf and Bracket_Drawings.dxf. All files carry revision A and use the same dimensions: base plate 100 x 60 mm, flange 35 mm, four dia 5.5 mm holes at 80 x 36 mm spacing, material and thickness stated in the BOM.
```

## 5. Milestone: flat-pattern DXF (attach `samples/relay_bracket_flat_pattern.dxf`, should pass)

```
Delivered: flat-pattern DXF file (relay_bracket_flat_pattern.dxf) for the 5052-H32 aluminium relay bracket, 1.5 mm thick. The file is in millimetres, all geometry is on a single layer, and it contains only the cut outline, the four holes and the bend line, with no other entities. Flat pattern 100 mm x 92.1 mm overall. Exactly four circular mounting holes, diameter 5.5 mm, with centres at (10, 12), (90, 12), (10, 48) and (90, 48) mm from the bottom-left corner. The cut outline is one closed polyline measuring 100 mm x 92.1 mm, the four holes are circle entities, and the bend line is a single line entity from (0, 57) to (100, 57), a continuous line across the full 100 mm width.
```

## 6. Negative test: a good description with the wrong file (should fail)

Paste the description from section 5, but attach `samples/relay_bracket_flat_pattern_WRONG.dxf` instead. The text claims four 5.5 mm holes, one layer and a bend line at y = 57 mm, but the file has only three holes (one is 8 mm), a second layer, and the bend line at y = 50 mm. The server measures the file, so the review fails and cites the measured values, even though the written description is perfect.
