/*
 * esp-hid-keyboard.ino — ESP32-S3 USB 复合设备：串口 + 完整 HID 键盘
 * 电脑通过 USB 串口发文本命令，ESP32-S3 模拟任意按键(字母/数字/功能/方向/组合键)
 *
 * 指令协议(串口 115200，每行一条命令，以换行结尾)：
 *   KEY <键>            点按一次(按下后自动松开)，如  KEY a / KEY enter / KEY ctrl+c
 *   TAP <键>            同 KEY
 *   PRESS <键>          按住(保持)，如 PRESS left / PRESS ctrl+shift+a
 *   HOLD <键>           同 PRESS
 *   RELEASE             松开全部按键
 *   RELEASE <键>        松开指定键(如 RELEASE ctrl)
 *   HELP                打印键名帮助
 * 兼容旧协议(单字符行)：L=按住左 R=按住右 S=松开全部
 *
 * 键名支持: 字母a-z 数字0-9 及 special: enter esc tab space backspace delete
 *   home end pgup pgdn up down left right caps f1..f12
 *   修饰: ctrl shift alt win(或gui)
 * 组合用 '+' 连接: ctrl+shift+z
 */
#include "USB.h"
#include "USBHIDKeyboard.h"

USBHIDKeyboard Keyboard;

String lineBuf = "";
unsigned long pressReleaseAt = 0;

// ---------- 按键状态跟踪(避免点按松开别人按住的键) ----------
#define MAXKEYS 16
uint8_t holdKeys[MAXKEYS]; int holdN = 0;   // PRESS 按住、需要保持的键
uint8_t tapKeys[MAXKEYS];  int tapN = 0;    // KEY 点按、到期要松开的键
unsigned long tapAt = 0;

bool inKeys(uint8_t* list, int n, uint8_t k) {
  for (int i = 0; i < n; i++) if (list[i] == k) return true;
  return false;
}
void addHold(uint8_t k) {
  if (!inKeys(holdKeys, holdN, k) && holdN < MAXKEYS) holdKeys[holdN++] = k;
}
void dropHold(uint8_t k) {
  for (int i = 0; i < holdN; i++) {
    if (holdKeys[i] == k) { holdKeys[i] = holdKeys[holdN - 1]; holdN--; return; }
  }
}
void clearAllState() { holdN = 0; tapN = 0; tapAt = 0; pressReleaseAt = 0; }

// ---------- 键名 -> 键码 ----------
uint8_t keyCode(const String& n) {
  String k = n; k.toLowerCase(); k.trim();
  if (k.length() == 1) {
    char c = k[0];
    if (c >= 'a' && c <= 'z') return (uint8_t)c;
    if (c >= '0' && c <= '9') return (uint8_t)c;
    if (c == ' ') return KEY_SPACE;
  }
  if (k == "enter" || k == "return") return KEY_RETURN;
  if (k == "esc" || k == "escape")  return KEY_ESC;
  if (k == "tab")                   return KEY_TAB;
  if (k == "space")                 return KEY_SPACE;
  if (k == "backspace" || k=="back")return KEY_BACKSPACE;
  if (k == "delete" || k == "del")  return KEY_DELETE;
  if (k == "home")                  return KEY_HOME;
  if (k == "end")                   return KEY_END;
  if (k == "pgup" || k == "pageup") return KEY_PAGE_UP;
  if (k == "pgdn" || k == "pagedown")return KEY_PAGE_DOWN;
  if (k == "up" || k == "uparrow")  return KEY_UP_ARROW;
  if (k == "down" || k == "downarrow") return KEY_DOWN_ARROW;
  if (k == "left" || k == "leftarrow") return KEY_LEFT_ARROW;
  if (k == "right" || k == "rightarrow") return KEY_RIGHT_ARROW;
  if (k == "caps" || k == "capslock") return KEY_CAPS_LOCK;
  if (k == "f1") return KEY_F1;  if (k == "f2") return KEY_F2;
  if (k == "f3") return KEY_F3;  if (k == "f4") return KEY_F4;
  if (k == "f5") return KEY_F5;  if (k == "f6") return KEY_F6;
  if (k == "f7") return KEY_F7;  if (k == "f8") return KEY_F8;
  if (k == "f9") return KEY_F9;  if (k == "f10") return KEY_F10;
  if (k == "f11") return KEY_F11; if (k == "f12") return KEY_F12;
  // 修饰键
  if (k == "ctrl" || k == "control")  return KEY_LEFT_CTRL;
  if (k == "shift")                   return KEY_LEFT_SHIFT;
  if (k == "alt")                     return KEY_LEFT_ALT;
  if (k == "win" || k == "gui" || k == "super") return KEY_LEFT_GUI;
  return 0;  // 未知
}

// ---------- 命令执行 ----------
void pressCombo(String arg, bool tap) {
  int start = 0;
  String parts[8];
  int np = 0;
  while (true) {
    int plus = arg.indexOf('+', start);
    String seg = (plus < 0) ? arg.substring(start) : arg.substring(start, plus);
    seg.trim();
    if (seg.length()) parts[np++] = seg;
    if (plus < 0) break;
    start = plus + 1;
  }
  uint8_t keys[8]; int nk = 0;
  for (int i = 0; i < np && i < 8; i++) {
    uint8_t c = keyCode(parts[i]);
    if (c) keys[nk++] = c;
  }
  if (nk == 0) { Keyboard.releaseAll(); clearAllState(); return; }
  for (int i = 0; i < nk; i++) Keyboard.press(keys[i]);  // 全部按下(修饰+主键)
  if (tap) {
    // 只记录本次点按需要松开的键；被别人按住的键不记录，到期也不会松开它
    tapN = 0;
    for (int i = 0; i < nk; i++) {
      if (!inKeys(holdKeys, holdN, keys[i]) && tapN < MAXKEYS) tapKeys[tapN++] = keys[i];
    }
    if (tapN > 0) tapAt = millis() + 60;  // 60ms 后只松开这几颗键
  } else {
    for (int i = 0; i < nk; i++) addHold(keys[i]);
  }
}

void releaseCombo(String arg) {
  int start = 0;
  while (true) {
    int plus = arg.indexOf('+', start);
    String seg = (plus < 0) ? arg.substring(start) : arg.substring(start, plus);
    seg.trim();
    if (seg.length()) {
      uint8_t c = keyCode(seg);
      if (c) { Keyboard.release(c); dropHold(c); }
    }
    if (plus < 0) break;
    start = plus + 1;
  }
}

void printHelp() {
  USBSerial.println("== ESP32 HID Keyboard 帮助 ==");
  USBSerial.println("KEY a | KEY enter | PRESS left | PRESS ctrl+shift+z");
  USBSerial.println("RELEASE | RELEASE ctrl | HELP");
  USBSerial.println("键: a-z 0-9 enter esc tab space backspace delete home end");
  USBSerial.println("    pgup pgdn up down left right caps f1-f12");
  USBSerial.println("修饰: ctrl shift alt win  (组合用+连接)");
}

void execLine(String line) {
  line.trim();
  if (line.length() == 0) return;
  // 兼容旧协议：单字符 L/R/S
  if (line.length() == 1) {
    char c = line[0];
    if (c == 'L' || c == 'l') { Keyboard.press(KEY_LEFT_ARROW); addHold(KEY_LEFT_ARROW); USBSerial.println("LEFT DOWN"); return; }
    if (c == 'R' || c == 'r') { Keyboard.press(KEY_RIGHT_ARROW); addHold(KEY_RIGHT_ARROW); USBSerial.println("RIGHT DOWN"); return; }
    if (c == 'S' || c == 's') { Keyboard.releaseAll(); clearAllState(); USBSerial.println("ALL RELEASED"); return; }
  }
  int sp = line.indexOf(' ');
  String cmd = line; String arg = "";
  if (sp >= 0) { cmd = line.substring(0, sp); arg = line.substring(sp + 1); arg.trim(); }
  cmd.toUpperCase();
  if (cmd == "KEY" || cmd == "TAP") {
    pressCombo(arg, true);
    USBSerial.println("TAP " + arg);
  } else if (cmd == "PRESS" || cmd == "HOLD") {
    pressCombo(arg, false);
    USBSerial.println("PRESS " + arg);
  } else if (cmd == "RELEASE") {
    if (arg.length() == 0) { Keyboard.releaseAll(); clearAllState(); USBSerial.println("ALL RELEASED"); }
    else { releaseCombo(arg); USBSerial.println("RELEASE " + arg); }
  } else if (cmd == "HELP") {
    printHelp();
  } else {
    USBSerial.println("? 未知命令: " + line);
  }
}

void setup() {
  USB.begin();
  Keyboard.begin();
  USBSerial.begin(115200);
  USBSerial.println("ESP32-S3 FULL Keyboard ready. Send HELP for commands.");
}

void loop() {
  // 逐字符读串口，拼行
  while (USBSerial.available()) {
    char c = (char)USBSerial.read();
    if (c == '\n' || c == '\r') {
      if (lineBuf.length()) {
        execLine(lineBuf);
        lineBuf = "";
      }
    } else {
      lineBuf += c;
    }
  }
  // 点按到期：只松开本次点按的键，绝不动别人按住的键
  if (tapAt && (long)(millis() - tapAt) >= 0) {
    for (int i = 0; i < tapN; i++) {
      if (!inKeys(holdKeys, holdN, tapKeys[i])) Keyboard.release(tapKeys[i]);
    }
    tapN = 0;
    tapAt = 0;
  }
  delay(1);
}
