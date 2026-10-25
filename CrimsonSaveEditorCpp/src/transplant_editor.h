#pragma once
#include "parc_engine.h"
#include <string>

namespace TransplantEditor {

void RenderTransplantTab(ParcEngine::SaveTree& tree, bool& dirty, const std::string& savePath);

} // namespace TransplantEditor
