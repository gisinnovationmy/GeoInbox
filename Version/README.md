# GeoInbox - QGIS Plugin for Email-Based GIS Data Exchange


## Overview

GeoInbox helps organisations transmit and prepare governed field-to-office-to-field GIS data inside QGIS. Receive data by email, a GISimple connection, or GeoVirtuallis GEOV (`.geov`) packages from MiniGISimple. Validate it against reference layers, review the differences, and merge approved changes into existing GeoPackage or PostGIS datasets.

> **Plugin status: experimental .

The plugin will be available from the official repository, [QGIS plugins page](https://plugins.qgis.org/).
Use the QGIS Plugins menu to install GeoInbox, [QGIS manual] (https://docs.qgis.org/latest/en/docs/user_manual/plugins/plugins.html).
Developed with: QGIS 4.0 - 4.99 (Qt6)


## Key Features

- **Email browser** - IMAP/POP3 support, automatic detection of GIS attachments (GeoJSON only), ZIP extraction, and offline message caching.
- **Data versioning** - compare client and host layers (GeoPackage, PostGIS) by ID, geometry and attributes to detect additions, deletions and modifications.
- **Diff layers** - color-coded visualization of changes on the QGIS map.
- **Controlled merge** - transactional commits with automatic GeoJSON backup, undo/redo and conflict resolution strategies.
- **GISimple integration** - direct authentication, group dataset browsing, and file upload/download.
- **Geov format and MiniGISimple** - import/export `.geov` ZIP packages by drag and drop, browse, or clipboard paste (for example, from WhatsApp). Interacts with data from MiniGISimple Firefox add-on and the GeoVirtuallis Android app.
- **email Trust system** - configurable trust settings for email attachments and senders, with auto-trust for GISimple group members.
- **Secure credentials** - stored in the QGIS Credential Manager.


## Requirements

- QGIS 4.0 - 4.99


## Getting Started

The user manual for GeoInbox is available here: [GeoInbox Documentation](http://gis.com.my/geoinbox/)


## Version

Current Version: 0.9.0
Last updated: October 2026