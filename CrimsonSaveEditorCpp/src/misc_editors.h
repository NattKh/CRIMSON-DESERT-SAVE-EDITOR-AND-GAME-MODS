#pragma once
#include "parc_engine.h"
#include <string>
#include <vector>
#include <cstdint>

namespace StoreEditor {
void ScanStores(ParcEngine::SaveTree& tree);
void RenderStoreTab(ParcEngine::SaveTree& tree, bool& dirty);
}

namespace WaypointEditor {
void ScanWaypoints(ParcEngine::SaveTree& tree);
void RenderWaypointTab(ParcEngine::SaveTree& tree, bool& dirty);
}

namespace MiscEditor {
void ScanMisc(ParcEngine::SaveTree& tree);
void RenderMiscTab(ParcEngine::SaveTree& tree, bool& dirty);
}
