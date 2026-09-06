cmake_minimum_required(VERSION 3.24)

project(CrimsonDesertSaveEditorDesktop LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(BUILD_GUI "Build Qt GUI editor" OFF)

if(BUILD_GUI)
    set(CMAKE_AUTOMOC ON)
    set(CMAKE_AUTORCC ON)
    set(CMAKE_AUTOUIC ON)
    find_package(Qt6 6.5 REQUIRED COMPONENTS Widgets)
endif()

add_library(save_parser_core STATIC
    src/save_parser_cpp.cpp
)

target_include_directories(save_parser_core
    PUBLIC
        src
)

target_link_libraries(save_parser_core
    PUBLIC
        bcrypt
)

set_target_properties(save_parser_core PROPERTIES
    INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE
    INTERPROCEDURAL_OPTIMIZATION_RELWITHDEBINFO TRUE
)

if(MSVC)
    target_compile_options(save_parser_core PRIVATE
        $<$<CONFIG:Release>:/O2 /GL /DNDEBUG>
        $<$<CONFIG:RelWithDebInfo>:/O2 /GL>
    )
    target_link_options(save_parser_core PRIVATE
        $<$<CONFIG:Release>:/LTCG>
        $<$<CONFIG:RelWithDebInfo>:/LTCG>
    )
else()
    target_compile_options(save_parser_core PRIVATE
        $<$<CONFIG:Release>:-O3 -DNDEBUG>
        $<$<CONFIG:RelWithDebInfo>:-O2>
    )
    target_link_options(save_parser_core PRIVATE
        $<$<CONFIG:Release>:-s>
    )
endif()

if(BUILD_GUI)
    add_executable(crimson_save_editor
        src/main.cpp
        src/MainWindow.cpp
        src/MainWindow.h
    )
    target_include_directories(crimson_save_editor PRIVATE src)
    target_link_libraries(crimson_save_editor PRIVATE Qt6::Widgets save_parser_core)
    set_target_properties(crimson_save_editor PROPERTIES
        WIN32_EXECUTABLE TRUE
        OUTPUT_NAME "crimson_save_editor"
    )
endif()

# Vendored LZ4 (for compression in save_writer)
add_library(lz4_lib STATIC
    third_party/lz4/lz4.c
    third_party/lz4/lz4hc.c
)
target_include_directories(lz4_lib PUBLIC third_party/lz4)
set_target_properties(lz4_lib PROPERTIES POSITION_INDEPENDENT_CODE ON)

# Save writer (compress + encrypt + write-back)
add_library(save_writer STATIC
    src/save_writer.cpp
)
target_include_directories(save_writer PUBLIC src)
target_link_libraries(save_writer PUBLIC bcrypt lz4_lib)

# parc_dump â€” CLI save tree dumper
add_executable(parc_dump src/parc_dump.cpp)
target_link_libraries(parc_dump PRIVATE save_parser_core)

# PARC serializer (full reserialize from tree)
add_library(parc_serializer STATIC src/parc_serializer.cpp)
target_include_directories(parc_serializer PUBLIC src)

# Item factory (construct items from scratch)
add_library(item_factory STATIC src/item_factory.cpp)
target_include_directories(item_factory PUBLIC src)

# PARC engine (core manipulation library)
add_library(parc_engine STATIC src/parc_engine.cpp)
target_include_directories(parc_engine PUBLIC src)
target_link_libraries(parc_engine PUBLIC save_parser_core save_writer parc_serializer)

# Native C ABI used by the PySide6 standalone editor.  Keep this boundary
# deliberately small and JSON-based so C++ exceptions and STL ownership never
# cross into Python.
add_library(parc_parser SHARED src/parc_dll.cpp)
target_include_directories(parc_parser PRIVATE src)
target_link_libraries(parc_parser PRIVATE parc_engine save_parser_core save_writer parc_serializer)
set_target_properties(parc_parser PROPERTIES OUTPUT_NAME "parc_parser")

# parc_xml — lossless XML export/import (vendored pugixml)
add_library(parc_xml STATIC src/parc_xml.cpp src/pugixml.cpp)
target_include_directories(parc_xml PUBLIC src)
target_link_libraries(parc_xml PUBLIC parc_engine)

# parc_engine_cli â€” CLI for PARC manipulation (insert, delete, write-back)
add_executable(parc_engine_cli src/parc_engine_cli.cpp)
target_include_directories(parc_engine_cli PRIVATE ${CMAKE_SOURCE_DIR}/third_party)
target_link_libraries(parc_engine_cli PRIVATE parc_engine item_factory parc_xml)
if(MSVC)
    target_link_options(parc_engine_cli PRIVATE /STACK:8388608)
endif()

# Icon cache (WIC-based webp loader + DX11 texture)
add_library(icon_cache STATIC src/icon_cache.cpp)
target_include_directories(icon_cache PUBLIC src)
target_link_libraries(icon_cache PUBLIC d3d11 windowscodecs)

# ImGui inventory editor — Win32 + DirectX11
set(IMGUI_DIR ${CMAKE_SOURCE_DIR}/third_party/imgui)
add_executable(inventory_editor WIN32
    src/inventory_editor.cpp
    src/quest_editor.cpp
    src/knowledge_editor.cpp
    src/dye_editor.cpp
    src/world_editor.cpp
    src/appearance_editor.cpp
    src/editor_common.cpp
    src/save_repair.cpp
    src/transplant_editor.cpp
    ${IMGUI_DIR}/imgui.cpp
    ${IMGUI_DIR}/imgui_draw.cpp
    ${IMGUI_DIR}/imgui_tables.cpp
    ${IMGUI_DIR}/imgui_widgets.cpp
    ${IMGUI_DIR}/backends/imgui_impl_win32.cpp
    ${IMGUI_DIR}/backends/imgui_impl_dx11.cpp
)
target_include_directories(inventory_editor PRIVATE
    src
    ${IMGUI_DIR}
    ${IMGUI_DIR}/backends
    ${CMAKE_SOURCE_DIR}/third_party
)
target_link_libraries(inventory_editor PRIVATE
    parc_engine
    parc_xml
    item_factory
    icon_cache
    d3d11
    dxgi
    d3dcompiler
    comdlg32
)
# 8MB stack (default is 1MB — too small for deep tree walks on 183K+ nodes)
if(MSVC)
    target_link_options(inventory_editor PRIVATE /STACK:8388608)
endif()
