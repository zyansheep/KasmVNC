/* Copyright (C) 2002-2005 RealVNC Ltd.  All Rights Reserved.
 * Copyright 2011 Pierre Ossman for Cendio AB
 * 
 * This is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 * 
 * This software is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU General Public License
 * along with this software; if not, write to the Free Software
 * Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307,
 * USA.
 */
#include <stdio.h>
#include <string.h>
#include <rfb/Exception.h>
#include <rfb/Security.h>
#include <rfb/fenceTypes.h>
#include <rfb/SMsgReader.h>
#include <rfb/SMsgWriter.h>
#include <rfb/SConnection.h>
#include <rfb/ServerCore.h>
#include <rfb/clipboardTypes.h>
#include <rfb/util.h>
#include <rfb/encodings.h>
#include <rfb/EncodeManager.h>
#include <rfb/SSecurity.h>

#include <rfb/LogWriter.h>

using namespace rfb;

static LogWriter vlog("SConnection");

// AccessRights values
const SConnection::AccessRights SConnection::AccessView           = 0x0001;
const SConnection::AccessRights SConnection::AccessKeyEvents      = 0x0002;
const SConnection::AccessRights SConnection::AccessPtrEvents      = 0x0004;
const SConnection::AccessRights SConnection::AccessCutText        = 0x0008;
const SConnection::AccessRights SConnection::AccessSetDesktopSize = 0x0010;
const SConnection::AccessRights SConnection::AccessNonShared      = 0x0020;
const SConnection::AccessRights SConnection::AccessDefault        = 0x03ff;
const SConnection::AccessRights SConnection::AccessNoQuery        = 0x0400;
const SConnection::AccessRights SConnection::AccessFull           = 0xffff;


SConnection::SConnection()
  : readyForSetColourMapEntries(false),
    is(0), os(0), reader_(0), writer_(0),
    ssecurity(0), state_(RFBSTATE_UNINITIALISED),
    preferredEncoding(encodingRaw),
    clientClipboard(NULL), hasLocalClipboard(false), sentClipboardCaps(false)
{
  defaultMajorVersion = 3;
  defaultMinorVersion = 8;
  if (rfb::Server::protocol3_3)
    defaultMinorVersion = 3;

  cp.setVersion(defaultMajorVersion, defaultMinorVersion);

  udps = new network::UdpStream;
}

SConnection::~SConnection()
{
  if (ssecurity) ssecurity->destroy();
  delete reader_;
  reader_ = 0;
  delete writer_;
  writer_ = 0;
  strFree(clientClipboard);
  delete udps;
}

void SConnection::setStreams(rdr::InStream* is_, rdr::OutStream* os_)
{
  is = is_;
  os = os_;
}

void SConnection::initialiseProtocol()
{
  cp.writeVersion(os);
  state_ = RFBSTATE_PROTOCOL_VERSION;
}

void SConnection::processMsg()
{
  switch (state_) {
  case RFBSTATE_PROTOCOL_VERSION: processVersionMsg();      break;
  case RFBSTATE_SECURITY_TYPE:    processSecurityTypeMsg(); break;
  case RFBSTATE_SECURITY:         processSecurityMsg();     break;
  case RFBSTATE_INITIALISATION:   processInitMsg();         break;
  case RFBSTATE_NORMAL:           reader_->readMsg();       break;
  case RFBSTATE_QUERYING:
    throw Exception("SConnection::processMsg: bogus data from client while "
                    "querying");
  case RFBSTATE_UNINITIALISED:
    throw Exception("SConnection::processMsg: not initialised yet?");
  default:
    throw Exception("SConnection::processMsg: invalid state");
  }
}

void SConnection::processVersionMsg()
{
  vlog.debug("reading protocol version");
  bool done;
  if (!cp.readVersion(is, &done)) {
    state_ = RFBSTATE_INVALID;
    throw Exception("reading version failed: not an RFB client?");
  }
  if (!done) return;

  vlog.info("Client needs protocol version %d.%d",
            cp.majorVersion, cp.minorVersion);

  if (cp.majorVersion != 3) {
    // unknown protocol version
    throwConnFailedException("Client needs protocol version %d.%d, server has %d.%d",
                             cp.majorVersion, cp.minorVersion,
                             defaultMajorVersion, defaultMinorVersion);
  }

  if (cp.minorVersion != 3 && cp.minorVersion != 7 && cp.minorVersion != 8) {
    vlog.error("Client uses unofficial protocol version %d.%d",
               cp.majorVersion,cp.minorVersion);
    if (cp.minorVersion >= 8)
      cp.minorVersion = 8;
    else if (cp.minorVersion == 7)
      cp.minorVersion = 7;
    else
      cp.minorVersion = 3;
    vlog.error("Assuming compatibility with version %d.%d",
               cp.majorVersion,cp.minorVersion);
  }

  versionReceived();

  std::list<rdr::U8> secTypes;
  std::list<rdr::U8>::iterator i;
  secTypes = security.GetEnabledSecTypes();

  if (cp.isVersion(3,3)) {

    // cope with legacy 3.3 client only if "no authentication" or "vnc
    // authentication" is supported.
    for (i=secTypes.begin(); i!=secTypes.end(); ++i) {
      if (*i == secTypeNone || *i == secTypeVncAuth) break;
    }
    if (i == secTypes.end()) {
      throwConnFailedException("No supported security type for %d.%d client",
                               cp.majorVersion, cp.minorVersion);
    }

    os->writeU32(*i);
    if (*i == secTypeNone) os->flush();
    state_ = RFBSTATE_SECURITY;
    ssecurity = security.GetSSecurity(*i);
    processSecurityMsg();
    return;
  }

  // list supported security types for >=3.7 clients

  if (secTypes.empty())
    throwConnFailedException("No supported security types");

  os->writeU8(secTypes.size());
  for (i=secTypes.begin(); i!=secTypes.end(); ++i)
    os->writeU8(*i);
  os->flush();
  state_ = RFBSTATE_SECURITY_TYPE;
}


void SConnection::processSecurityTypeMsg()
{
  vlog.debug("processing security type message");
  int secType = is->readU8();

  processSecurityType(secType);
}

void SConnection::processSecurityType(int secType)
{
  // Verify that the requested security type should be offered
  std::list<rdr::U8> secTypes;
  std::list<rdr::U8>::iterator i;

  secTypes = security.GetEnabledSecTypes();
  for (i=secTypes.begin(); i!=secTypes.end(); ++i)
    if (*i == secType) break;
  if (i == secTypes.end())
    throw Exception("Requested security type not available");

  vlog.info("Client requests security type %s(%d)",
            secTypeName(secType),secType);

  try {
    state_ = RFBSTATE_SECURITY;
    ssecurity = security.GetSSecurity(secType);
  } catch (rdr::Exception& e) {
    throwConnFailedException("%s", e.str());
  }

  processSecurityMsg();
}

void SConnection::processSecurityMsg()
{
  vlog.debug("processing security message");
  try {
    bool done = ssecurity->processMsg(this);
    if (done) {
      state_ = RFBSTATE_QUERYING;
      //setAccessRights(ssecurity->getAccessRights());
      queryConnection(ssecurity->getUserName());
    }
  } catch (AuthFailureException& e) {
    vlog.error("AuthFailureException: %s", e.str());
    os->writeU32(secResultFailed);
    if (!cp.beforeVersion(3,8)) // 3.8 onwards have failure message
      os->writeString(e.str());
    os->flush();
    throw;
  }
}

void SConnection::processInitMsg()
{
  vlog.debug("reading client initialisation");
  reader_->readClientInit();
}

void SConnection::throwConnFailedException(const char* format, ...)
{
	va_list ap;
	char str[256];

	va_start(ap, format);
	(void) vsnprintf(str, sizeof(str), format, ap);
	va_end(ap);

  vlog.info("Connection failed: %s", str);

  if (state_ == RFBSTATE_PROTOCOL_VERSION) {
    if (cp.majorVersion == 3 && cp.minorVersion == 3) {
      os->writeU32(0);
      os->writeString(str);
      os->flush();
    } else {
      os->writeU8(0);
      os->writeString(str);
      os->flush();
    }
  }

  state_ = RFBSTATE_INVALID;
  throw ConnFailedException(str);
}

void SConnection::writeConnFailedFromScratch(const char* msg,
                                             rdr::OutStream* os)
{
  os->writeBytes("RFB 003.003\n", 12);
  os->writeU32(0);
  os->writeString(msg);
  os->flush();
}

void SConnection::setEncodings(int nEncodings, const rdr::S32 *encodings) {
    preferredEncoding = encodingRaw;
    for (int i = 0; i < nEncodings; i++) {
        if (EncodeManager::supported(encodings[i])) {
            preferredEncoding = encodings[i];
            break;
        }
    }

    SMsgHandler::setEncodings(nEncodings, encodings);

    // A standard client that speaks the extended clipboard gets its text as UTF-8 that way: the server says what
    // it can do once (TigerVNC's server does the same), and from then on ServerCutText is never latin-1 for it.
    if (!sentClipboardCaps && !cp.kasmClient() && cp.supportsExtendedClipboard) {
      rdr::U32 sizes[] = { rfb::Server::DLP_ClipAcceptMax ? (rdr::U32) rfb::Server::DLP_ClipAcceptMax : 20 * 1024 * 1024 };
      writer()->writeClipboardCaps(clipboardUTF8 | clipboardRequest | clipboardPeek | clipboardNotify | clipboardProvide,
                                   sizes);
      sentClipboardCaps = true;
    }
}

void SConnection::handleClipboardCaps(rdr::U32 flags, const rdr::U32* lengths)
{
  cp.setClipboardCaps(flags, lengths);
}

// Whether bytes are well-formed UTF-8 (no overlong forms, surrogates or code points past U+10FFFF).
static bool validUTF8(const unsigned char* p, size_t len)
{
  size_t i = 0;
  while (i < len) {
    unsigned char c = p[i];
    size_t n;
    unsigned cp;
    if (c < 0x80) { i++; continue; }
    else if ((c & 0xe0) == 0xc0) { n = 1; cp = c & 0x1f; }
    else if ((c & 0xf0) == 0xe0) { n = 2; cp = c & 0x0f; }
    else if ((c & 0xf8) == 0xf0) { n = 3; cp = c & 0x07; }
    else return false;
    if (i + n >= len) return false;
    for (size_t k = 1; k <= n; k++) {
      if ((p[i + k] & 0xc0) != 0x80) return false;
      cp = (cp << 6) | (p[i + k] & 0x3f);
    }
    if ((n == 1 && cp < 0x80) || (n == 2 && cp < 0x800) || (n == 3 && cp < 0x10000) ||
        cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
      return false;
    i += n + 1;
  }
  return true;
}

static const SConnection::binaryClipboard_t* textOf(const std::vector<SConnection::binaryClipboard_t>& clip)
{
  for (const auto& b : clip) {
    if (!strcmp(b.mime, "text/plain") && !b.data.empty())
      return &b;
  }
  return NULL;
}

void SConnection::handleClipboardRequest(rdr::U32 flags)
{
  const binaryClipboard_t* text = textOf(binaryClipboard);
  if (!(flags & clipboardUTF8) || !text)
    return;
  // The text with its NUL, as the extended clipboard carries it.
  std::vector<rdr::U8> buf(text->data.begin(), text->data.end());
  buf.push_back(0);
  size_t lengths[] = { buf.size() };
  const rdr::U8* data[] = { &buf[0] };
  writer()->writeClipboardProvide(clipboardUTF8, lengths, data);
}

void SConnection::handleClipboardPeek(rdr::U32)
{
  writer()->writeClipboardNotify(textOf(binaryClipboard) ? clipboardUTF8 : 0);
}

void SConnection::handleClipboardNotify(rdr::U32 flags)
{
  // The client's clipboard has text: ask for it (it comes as handleClipboardProvide).
  if (flags & clipboardUTF8)
    writer()->writeClipboardRequest(clipboardUTF8);
}

void SConnection::handleClipboardProvide(rdr::U32 flags, const size_t* lengths, const rdr::U8* const* data)
{
  if (!(flags & clipboardUTF8))
    return;
  // The text is first among the formats; it ends at its NUL.
  size_t len = strnlen((const char*) data[0], lengths[0]);
  if (!validUTF8(data[0], len)) {
    vlog.error("Invalid UTF-8 in the client's clipboard - ignoring");
    return;
  }
  char mimes[1][32] = { "text/plain" };
  clearBinaryClipboard();
  addBinaryClipboard(mimes[0], data[0], len, 0);
  handleClipboardAnnounceBinary(1, mimes);
}

void SConnection::clearBinaryClipboard()
{
  binaryClipboard.clear();
}

void SConnection::addBinaryClipboard(const char mime[], const rdr::U8 *data,
                                     const rdr::U32 len, const rdr::U32 id)
{
  binaryClipboard_t bin;
  strncpy(bin.mime, mime, sizeof(bin.mime));
  bin.mime[sizeof(bin.mime) - 1] = '\0';

  bin.data.resize(len);
  memcpy(&bin.data[0], data, len);

  bin.id = id;

  binaryClipboard.push_back(bin);
}


void SConnection::supportsQEMUKeyEvent()
{
  writer()->writeQEMUKeyEvent();
}

void SConnection::versionReceived()
{
}

void SConnection::authSuccess()
{
}

void SConnection::queryConnection(const char* userName)
{
  approveConnection(true);
}

void SConnection::approveConnection(bool accept, const char* reason)
{
  if (state_ != RFBSTATE_QUERYING)
    throw Exception("SConnection::approveConnection: invalid state");

  if (!cp.beforeVersion(3,8) || ssecurity->getType() != secTypeNone) {
    if (accept) {
      os->writeU32(secResultOK);
    } else {
      os->writeU32(secResultFailed);
      if (!cp.beforeVersion(3,8)) { // 3.8 onwards have failure message
        if (reason)
          os->writeString(reason);
        else
          os->writeString("Authentication failure");
      }
    }
    os->flush();
  }

  if (accept) {
    state_ = RFBSTATE_INITIALISATION;
    reader_ = new SMsgReader(this, is);
    writer_ = new SMsgWriter(&cp, os, udps);
    authSuccess();
  } else {
    state_ = RFBSTATE_INVALID;
    if (reason)
      throw AuthFailureException(reason);
    else
      throw AuthFailureException();
  }
}

void SConnection::clientInit(bool shared)
{
  writer_->writeServerInit();
  state_ = RFBSTATE_NORMAL;
}

void SConnection::setPixelFormat(const PixelFormat& pf)
{
  SMsgHandler::setPixelFormat(pf);
  readyForSetColourMapEntries = true;
  if (!pf.trueColour)
    writeFakeColourMap();
}

void SConnection::framebufferUpdateRequest(const Rect& r, bool incremental)
{
  if (!readyForSetColourMapEntries) {
    readyForSetColourMapEntries = true;
    if (!cp.pf().trueColour) {
      writeFakeColourMap();
    }
  }
}

void SConnection::fence(rdr::U32 flags, unsigned len, const char data[])
{
  if (!(flags & fenceFlagRequest))
    return;

  // We cannot guarantee any synchronisation at this level
  flags = 0;

  writer()->writeFence(flags, len, data);
}

void SConnection::enableContinuousUpdates(bool enable,
                                          int x, int y, int w, int h)
{
}

void SConnection::handleClipboardAnnounce(bool available)
{
}

void SConnection::announceClipboard(bool available)
{
  hasLocalClipboard = available;
}

void SConnection::writeFakeColourMap(void)
{
  rdr::U16 red[256], green[256], blue[256];

  for (int i = 0;i < 256;i++)
    cp.pf().rgbFromPixel(i, &red[i], &green[i], &blue[i]);

  writer()->writeSetColourMapEntries(0, 256, red, green, blue);
}
