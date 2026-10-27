import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import serial
import serial.tools.list_ports


REG_SIZE = 5 * 1024  # 5KB = 5120 bytes
REG_FILE = "registers.bin"


def calculate_crc16_modbus(data: bytes) -> int:
  """Calculate standard CRC16-Modbus checksum (Polynomial: 0xA001)."""
  crc = 0xFFFF
  for byte in data:
    crc ^= byte
    for _ in range(8):
      if (crc & 0x0001) != 0:
        crc = (crc >> 1) ^ 0xA001
      else:
        crc >>= 1
  return crc


class RegisterStore:
  """Thread-safe persistent uint8 register map backed by a binary file."""

  def __init__(self, size: int, file_name: str):
    self.size = size
    self.file_name = file_name
    self.regs = bytearray(size)
    self.lock = threading.RLock()

  def load(self) -> None:
    with self.lock:
      if os.path.exists(self.file_name):
        try:
          with open(self.file_name, "rb") as f:
            data = f.read()
          if len(data) >= self.size:
            self.regs[:] = data[: self.size]
          else:
            self.regs[: len(data)] = data
          return
        except Exception:
          pass
      self.save()

  def save(self) -> None:
    with self.lock:
      with open(self.file_name, "wb") as f:
        f.write(self.regs)

  def write(self, start: int, data: bytes) -> None:
    """Write bytes to register map with boundary verification."""
    end = start + len(data)
    if start < 0 or end > self.size:
      raise ValueError(
          f"Register address out of range: start={start}, end={end}, max={self.size}"
      )
    with self.lock:
      self.regs[start:end] = data

  def read(self, start: int, length: int) -> bytes:
    """Read bytes from register map with boundary verification."""
    end = start + length
    if start < 0 or length < 0 or end > self.size:
      raise ValueError(
          f"Register address out of range: start={start}, end={end}, max={self.size}"
      )
    with self.lock:
      return bytes(self.regs[start:end])


class SerialApp:

  def __init__(self, root):
    self.root = root
    self.root.title("Embedded Serial Protocol Monitor & Echo (Registers)")
    self.root.geometry("900x640")

    self.serial_port = None
    self.is_running = False
    self.worker_thread = None

    self.registers = RegisterStore(REG_SIZE, REG_FILE)
    self.registers.load()

    self.init_ui()
    self.append_log(
        "INFO", f"Register file initialized: {REG_FILE} (Size: {REG_SIZE} bytes)"
    )

  def init_ui(self):
    main_frame = ttk.Frame(self.root, padding=10)
    main_frame.pack(fill=tk.BOTH, expand=True)

    config_frame = ttk.LabelFrame(main_frame, text="Serial Port Configuration")
    config_frame.pack(side=tk.TOP, fill=tk.X, pady=5)

    ttk.Label(config_frame, text="Port:").grid(
        row=0, column=0, padx=5, pady=5, sticky=tk.W
    )
    self.port_combo = ttk.Combobox(config_frame, width=32, state="readonly")
    self.port_combo.grid(row=0, column=1, padx=5, pady=5)

    self.btn_refresh = ttk.Button(
        config_frame, text="Refresh Ports", command=self.refresh_ports
    )
    self.btn_refresh.grid(row=0, column=2, padx=5, pady=5)

    ttk.Label(config_frame, text="Baudrate:").grid(
        row=0, column=3, padx=5, pady=5, sticky=tk.W
    )
    self.baud_combo = ttk.Combobox(
        config_frame,
        values=[
            "9600",
            "19200",
            "38400",
            "57600",
            "115200",
            "230400",
            "460800",
            "921600",
        ],
        width=10,
        state="readonly",
    )
    self.baud_combo.set("115200")
    self.baud_combo.grid(row=0, column=4, padx=5, pady=5)

    ttk.Label(config_frame, text="Data Bits:").grid(
        row=1, column=0, padx=5, pady=5, sticky=tk.W
    )
    self.data_combo = ttk.Combobox(
        config_frame, values=["5", "6", "7", "8"], width=8, state="readonly"
    )
    self.data_combo.set("8")
    self.data_combo.grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)

    ttk.Label(config_frame, text="Parity:").grid(
        row=1, column=2, padx=5, pady=5, sticky=tk.W
    )
    self.parity_combo = ttk.Combobox(
        config_frame,
        values=["None", "Even", "Odd", "Mark", "Space"],
        width=8,
        state="readonly",
    )
    self.parity_combo.set("None")
    self.parity_combo.grid(row=1, column=3, padx=5, pady=5, sticky=tk.W)

    ttk.Label(config_frame, text="Stop Bits:").grid(
        row=1, column=4, padx=5, pady=5, sticky=tk.W
    )
    self.stop_combo = ttk.Combobox(
        config_frame, values=["1", "1.5", "2"], width=8, state="readonly"
    )
    self.stop_combo.set("1")
    self.stop_combo.grid(row=1, column=5, padx=5, pady=5, sticky=tk.W)

    self.btn_toggle = tk.Button(
        config_frame,
        text="Start Listening",
        bg="#27ae60",
        fg="white",
        font=("Segoe UI", 10, "bold"),
        command=self.toggle_connection,
    )
    self.btn_toggle.grid(row=0, column=5, padx=10, pady=5, sticky=tk.EW)

    log_frame = ttk.LabelFrame(main_frame, text="Event & Packet Logs")
    log_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=5)

    self.log_text = tk.Text(
        log_frame,
        wrap=tk.WORD,
        bg="#1e1e1e",
        fg="#d4d4d4",
        font=("Consolas", 10),
    )
    self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    scrollbar = ttk.Scrollbar(
        log_frame, orient=tk.VERTICAL, command=self.log_text.yview
    )
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    self.log_text.config(yscrollcommand=scrollbar.set)

    self.log_text.tag_config("INFO", foreground="#3498db")
    self.log_text.tag_config("RX", foreground="#2ecc71")
    self.log_text.tag_config("TX", foreground="#9b59b6")
    self.log_text.tag_config("SUCCESS", foreground="#1abc9c")
    self.log_text.tag_config("WARNING", foreground="#f39c12")
    self.log_text.tag_config("ERROR", foreground="#e74c3c")
    self.log_text.tag_config("DEBUG", foreground="#95a5a6")

    btn_clear = ttk.Button(
        main_frame,
        text="Clear Log",
        command=lambda: self.log_text.delete("1.0", tk.END),
    )
    btn_clear.pack(anchor=tk.E, pady=2)

    self.refresh_ports()

  def refresh_ports(self):
    ports = serial.tools.list_ports.comports()
    port_list = [f"{p.device} - {p.description}" for p in ports]
    self.port_combo["values"] = port_list
    if port_list:
      self.port_combo.current(0)
    else:
      self.port_combo.set("")

  def append_log(self, tag, message):
    self.root.after(0, self._safe_append_log, tag, message)

  def _safe_append_log(self, tag, message):
    timestamp = time.strftime("[%H:%M:%S] ")
    self.log_text.insert(tk.END, timestamp, "DEBUG")
    self.log_text.insert(tk.END, f"[{tag}] ", tag)
    self.log_text.insert(tk.END, f"{message}\n")
    self.log_text.see(tk.END)

  def toggle_connection(self):
    if not self.is_running:
      raw_port = self.port_combo.get()
      if not raw_port:
        messagebox.showerror("Error", "No serial port selected.")
        return
      port_name = raw_port.split(" - ")[0].strip()
      baudrate = int(self.baud_combo.get())

      databits_map = {
          "5": serial.FIVEBITS,
          "6": serial.SIXBITS,
          "7": serial.SEVENBITS,
          "8": serial.EIGHTBITS,
      }
      databits = databits_map[self.data_combo.get()]

      parity_map = {
          "None": serial.PARITY_NONE,
          "Even": serial.PARITY_EVEN,
          "Odd": serial.PARITY_ODD,
          "Mark": serial.PARITY_MARK,
          "Space": serial.PARITY_SPACE,
      }
      parity = parity_map[self.parity_combo.get()]

      stopbits_map = {
          "1": serial.STOPBITS_ONE,
          "1.5": serial.STOPBITS_ONE_POINT_FIVE,
          "2": serial.STOPBITS_TWO,
      }
      stopbits = stopbits_map[self.stop_combo.get()]

      try:
        self.serial_port = serial.Serial(
            port=port_name,
            baudrate=baudrate,
            bytesize=databits,
            parity=parity,
            stopbits=stopbits,
            timeout=0.1,
        )
      except Exception as e:
        self.append_log("ERROR", f"Failed to open port: {str(e)}")
        return

      self.is_running = True
      self.btn_toggle.config(text="Stop Listening", bg="#c0392b")
      self.port_combo.config(state="readonly")
      self.append_log(
          "INFO", f"Port {port_name} opened successfully at {baudrate} baud."
      )

      self.worker_thread = threading.Thread(
          target=self.rx_worker, daemon=True
      )
      self.worker_thread.start()

    else:
      self.is_running = False
      if self.serial_port and self.serial_port.is_open:
        self.serial_port.close()
      self.btn_toggle.config(text="Start Listening", bg="#27ae60")
      self.port_combo.config(state="readonly")
      self.append_log("INFO", "Serial port closed.")

  def rx_worker(self):
    buffer = bytearray()
    while self.is_running:
      try:
        if self.serial_port and self.serial_port.in_waiting > 0:
          data = self.serial_port.read(self.serial_port.in_waiting)
          buffer.extend(data)

        while len(buffer) >= 4:
          # 1. Search for SOF (0xAA 0x55)
          sof_index = -1
          for i in range(len(buffer) - 1):
            if buffer[i] == 0xAA and buffer[i + 1] == 0x55:
              sof_index = i
              break

          if sof_index == -1:
            if len(buffer) > 1:
              buffer = buffer[-1:]
            break

          if sof_index > 0:
            garbage = buffer[:sof_index].hex(" ").upper()
            self.append_log(
                "WARNING", f"Discarded preamble garbage: {garbage}"
            )
            del buffer[:sof_index]

          if len(buffer) < 4:
            break

          # 2. Extract Length (Bytes 2 & 3, Little Endian)
          length_field = int.from_bytes(buffer[2:4], "little")
          total_packet_len = 2 + length_field

          if len(buffer) < total_packet_len:
            break

          frame = bytes(buffer[:total_packet_len])
          del buffer[:total_packet_len]

          self.append_log("RX", f"Frame Received: {frame.hex(' ').upper()}")

          # Minimum length validation
          if len(frame) < 13:
            self.append_log(
                "ERROR", "Frame too short for valid packet structure."
            )
            continue

          # 3. CRC Check (from Byte 2 onwards, excluding last 2 CRC bytes)
          payload_for_crc = frame[2:-2]
          received_crc = int.from_bytes(frame[-2:], "little")
          calculated_crc = calculate_crc16_modbus(payload_for_crc)

          self.append_log(
              "DEBUG",
              f"CRC Check: Received=0x{received_crc:04X},"
              f" Calculated=0x{calculated_crc:04X}",
          )

          if received_crc != calculated_crc:
            self.append_log(
                "ERROR",
                f"CRC Mismatch! Received: 0x{received_crc:04X} != Calculated:"
                f" 0x{calculated_crc:04X}",
            )
            continue

          # 4. Command Dispatching (Correct 0-based Python indices)
          # Index 6 -> Command Byte
          # Index 7..8 -> Start Address (Little Endian)
          # Index 9..10 -> Register Length (Little Endian)
          # Index 11.. -> Payload Data

          command = frame[6]

          if command == 0:
            # Length verification: len(frame) == length_field + 2
            if len(frame) != (length_field + 2):
              self.append_log(
                  "ERROR",
                  f"Length verification failed: len(frame)={len(frame)} !="
                  f" (length_field + 2)={length_field + 2}",
              )
              continue

            start_addr = int.from_bytes(frame[7:9], "little")
            reg_len = int.from_bytes(frame[9:11], "little")

            data_start = 11
            data_end = 11 + reg_len

            if data_end > (len(frame) - 2):
              self.append_log(
                  "ERROR",
                  f"Payload boundary error: expected {reg_len} bytes from"
                  f" offset 11, but available is {len(frame) - 2 - 11}.",
              )
              continue

            payload_data = frame[data_start:data_end]

            # Write to persistent registers
            try:
              self.registers.write(start_addr, payload_data)
              self.registers.save()
              self.append_log(
                  "SUCCESS",
                  f"Registers written: Start Addr=0x{start_addr:04X}"
                  f" ({start_addr}), Length={reg_len} bytes. Persisted to disk.",
              )
            except Exception as ex:
              self.append_log("ERROR", f"Failed to write registers: {str(ex)}")
              continue

            # Echo the received frame unchanged back as response
            try:
              self.serial_port.write(frame)
              self.append_log(
                  "TX", f"Response Echoed: {frame.hex(' ').upper()}"
              )
            except Exception as ex:
              self.append_log("ERROR", f"Failed to transmit echo: {str(ex)}")
              continue

          elif command == 0x01:
            # Read request: build response frame with payload from registers
            start_addr = frame[7] | (frame[8] << 8)
            reg_len = frame[9] | (frame[10] << 8)

            if reg_len == 0:
              self.append_log("WARNING", "Invalid read length: 0")
              continue

            if start_addr < 0 or start_addr >= self.registers.size:
              self.append_log(
                  "WARNING",
                  f"Read out of range: Start Addr=0x{start_addr:04X}",
              )
              continue

            if start_addr + reg_len > self.registers.size:
              self.append_log(
                  "WARNING",
                  f"Read out of range: Start=0x{start_addr:04X},"
                  f" Length={reg_len} (size={self.registers.size})",
              )
              continue

            try:
              payload = self.registers.read(start_addr, reg_len)
            except Exception as ex:
              self.append_log("ERROR", f"Failed to read registers: {str(ex)}")
              continue

            # Build response:
            # [0..1]  SOF
            # [2..3]  Length (LE) excluding SOF
            # [4..10] copied from request frame exactly (7 bytes: SEQ, CMD, ADDR, REGLEN)
            # [11..]  payload
            resp = bytearray()
            resp += b"\xAA\x55"
            resp += b"\x00\x00"  # placeholder for length
            resp += bytes(frame[4:11])
            resp += payload

            # Length = bytes from index 2 to end of payload + 2 CRC bytes
            # => excluding SOF = (2 LEN + 7 copied + payload + 2 CRC) = 11 + payload_len
            out_len = 11 + len(payload)
            resp[2] = out_len & 0xFF
            resp[3] = (out_len >> 8) & 0xFF

            # CRC over bytes from index 2 to end of payload
            crc = calculate_crc16_modbus(bytes(resp[2:]))
            resp += bytes([crc & 0xFF, (crc >> 8) & 0xFF])

            try:
              self.serial_port.write(bytes(resp))
              self.append_log(
                  "TX", f"Read Response Sent: {bytes(resp).hex(' ').upper()}"
              )
              self.append_log(
                  "SUCCESS",
                  f"Read served: Start Addr=0x{start_addr:04X}"
                  f" ({start_addr}), Length={reg_len} bytes",
              )
            except Exception as ex:
              self.append_log(
                  "ERROR", f"Failed to transmit read response: {str(ex)}"
              )
              continue


          else:
            self.append_log(
                "WARNING",
                f"Unhandled command 0x{command:02X}. No action taken.",
            )

        time.sleep(0.01)

      except Exception as e:
        if self.is_running:
          self.append_log("ERROR", f"Serial communication exception: {str(e)}")
        break


if __name__ == "__main__":
  root = tk.Tk()
  app = SerialApp(root)
  root.mainloop()
