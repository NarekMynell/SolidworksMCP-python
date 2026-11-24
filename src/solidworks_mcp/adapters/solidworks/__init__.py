"""Grouped SolidWorks mixins for the PyWin32 adapter."""

from .assembly import SolidWorksAssemblyMixin
from .configurations import SolidWorksConfigurationsMixin
from .documentation import SolidWorksDocumentationMixin
from .drawing_pack import SolidWorksDrawingPackMixin
from .drawing_standard import SolidWorksDrawingStandardMixin
from .equations import SolidWorksEquationsMixin
from .feature_tree import SolidWorksFeatureTreeMixin
from .features import SolidWorksFeaturesMixin
from .io import SolidWorksIOMixin
from .part_features import SolidWorksPartFeaturesMixin
from .selection import SolidWorksSelectionMixin
from .sheet_metal import SolidWorksSheetMetalMixin
from .sketch import SolidWorksSketchMixin
from .templates import SolidWorksTemplatesMixin
from .weldment_plates import SolidWorksWeldmentPlatesMixin
from .weldment_vents import SolidWorksWeldmentVentsMixin
from .weldments import SolidWorksWeldmentsMixin

__all__ = [
    "SolidWorksAssemblyMixin",
    "SolidWorksConfigurationsMixin",
    "SolidWorksDocumentationMixin",
    "SolidWorksDrawingPackMixin",
    "SolidWorksDrawingStandardMixin",
    "SolidWorksEquationsMixin",
    "SolidWorksFeatureTreeMixin",
    "SolidWorksFeaturesMixin",
    "SolidWorksIOMixin",
    "SolidWorksPartFeaturesMixin",
    "SolidWorksSelectionMixin",
    "SolidWorksSheetMetalMixin",
    "SolidWorksWeldmentPlatesMixin",
    "SolidWorksWeldmentVentsMixin",
    "SolidWorksWeldmentsMixin",
    "SolidWorksSketchMixin",
    "SolidWorksTemplatesMixin",
]
