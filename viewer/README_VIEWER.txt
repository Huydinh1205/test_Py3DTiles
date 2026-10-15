HOW TO OPEN THE VIEWER
======================

A browser refuses to load a 3D Tiles tileset from a file:// path, so the
folder has to be served over HTTP. One command does it.

1. Open a terminal in the test_Py3DTiles folder:

       cd test_Py3DTiles
       python -m http.server 8080

2. Open this URL in Chrome:

       http://localhost:8080/viewer/index.html

3. In the "tileset.json URL" box, point at the tileset produced by the test.
   The path is relative to the viewer folder, for example:

       ../results/sample_las14_pf7/tiles/tileset.json
       ../results/<your dataset name>/tiles/tileset.json

4. Press "Load tileset".


WHAT TO CHECK, AND WHY
======================

The assessment covers "visual quality, processing resources, output
size, browser performance and the amount of manual intervention needed".
The scripts cover everything except the last two visual items. This is where
those get answered.

  Colour by: Source RGB
      Does the point cloud look right? Compare against the orthomosaic.
      If everything is black, the source RGB is 8-bit and the conversion
      needs --color-scale 256.

  Colour by: Classification
      THIS IS THE IMPORTANT ONE. If the classification attribute survived
      the conversion you will see distinct colours per class and the legend
      toggles will filter the cloud. If every point disappears or stays one
      colour, the attribute did not survive. Script 04 proves this
      numerically; this is the visual confirmation.

  Colour by: Intensity
      Only meaningful if intensity was included in --extra-fields.
      The white point is set automatically from a sample tile (99th
      percentile of its intensity values), so it adapts to DJI Terra's
      0..65280 range as well as tools that use small values. Override it
      in the "Intensity white point" box: raise it if the image looks
      washed out, lower it if it looks dark.

  Attributes found
      The viewer reads one tile's header and lists the attributes it
      carries. If 'classification' is missing the status box turns red.

  3D Tiles 1.1 tilesets
      Loaded automatically through an UNLIT CustomShader, because
      Cesium's style language cannot read py3dtiles' 1.1 attributes and
      its 1.1 colours are stored un-normalised. See the README section
      "3D Tiles 1.0 vs 1.1". Expect the 1.1 points to look sparser.

  Performance panel
      Frames per second, points rendered and GPU memory, on the same
      machine and browser an analyst would use. Note the numbers at a
      typical working zoom level, not zoomed all the way out.

  Load time to idle
      How long from pressing Load until every visible tile has arrived.
      This is the number an analyst actually experiences.


NOTES
=====

- No Cesium ion account or token is required. The globe and base imagery are
  deliberately disabled, so nothing is requested from ion and the frame rate
  measures the tileset on its own.
- CesiumJS 1.145.0 is loaded from a CDN, so the machine needs internet access
  the first time. To run fully offline, download the Cesium build and change
  the two CDN URLs at the top of index.html to local paths.
- The production viewer will have terrain, imagery and other layers
  competing for the same GPU budget, so treat these numbers as an optimistic
  upper bound and say so in the assessment.
