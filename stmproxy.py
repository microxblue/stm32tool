# @file
#
#  Copyright (c) 2022, Microxblue. All rights reserved.
#  SPDX-License-Identifier: BSD-2-Clause-Patent
#

import os
import sys
import time
import argparse
import usb.core
import usb.util
import logging
import socket
import queue
from threading import Thread
from stmtalk import KbHit, STM32_USB_DEV, bytes_to_value, value_to_bytes, bytes_to_print_str

# A class that extends the Thread class
class UsbPipeThread(Thread):

    def __init__(self, rx_qu, tx_qu, quit, inf = 0, pid = STM32_USB_DEV.MY_PID):
        Thread.__init__(self)
        self.rx_qu = rx_qu
        self.tx_qu = tx_qu
        self.quit  = quit
        self.inf   = inf
        self.pid   = pid

    def run(self):
        while not self.quit[0]:
            try:
                self.stm_usb = STM32_USB_DEV('', self.pid, self.inf)
            except:
                self.stm_usb = None

            if self.stm_usb is None or self.stm_usb.epout is None or self.stm_usb.epin is None:
                time.sleep (3)
                continue

            rx_buf = bytearray()
            logging.info ('=> USB started running (inf:%d)' % self.inf)
            while not self.quit[0]:
                if len (rx_buf) == 0:
                    try:
                        rx_buf = bytearray(self.stm_usb.read(64))
                    except usb.USBError as e:
                        err_str = repr(e)
                        if ('timeout error' in err_str) or ('timed out' in err_str):
                            rx_buf = bytearray()
                        else:
                            raise

                if len (rx_buf) > 0 and not self.tx_qu.full():
                    logging.debug ("USB interface %d RX: (len: %d)\n%s" % (self.inf, len(rx_buf), bytes_to_print_str (rx_buf, indent = 3)))
                    self.tx_qu.put (bytearray(rx_buf))
                    rx_buf.clear()

                if not self.rx_qu.empty():
                    rx_data = self.rx_qu.get()
                    logging.debug ('USB interface %d TX: (len: %d)\n%s' % (self.inf, len(rx_data), bytes_to_print_str (rx_data, indent = 3)))
                    self.stm_usb.write(rx_data)

            logging.info ('=> USB stopped running (inf:%d)' % self.inf)

            time.sleep (.3)

        if self.stm_usb is not None:
            self.stm_usb.close()

        logging.info  ("=> USB thread quit (inf:%d)" % self.inf)



class TcpThread(Thread):

    MAX_PKT = 1024

    def __init__(self, rx_qu, tx_qu, quit, host, port, stream = True):
        Thread.__init__(self)
        self.rx_qu  = rx_qu
        self.tx_qu  = tx_qu
        self.quit   = quit
        self.port   = port
        self.stream = stream

        self.socket = socket.socket()  # get instance
        self.socket.setsockopt (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # look closely. The bind() function takes tuple as argument
        self.socket.bind((host, port))  # bind host address and port together

        # configure how many client the server can listen simultaneously
        self.socket.listen(1)

    def run (self):
        while not self.quit[0]:
            self.socket.settimeout (.3)
            try:
                conn, address = self.socket.accept()  # accept new connection
            except socket.timeout as e:
                continue

            conn.setblocking(True)
            conn.settimeout(.05)

            rx_buf = bytearray ()
            logging.info ("=> Connection created with (%s:%d)" % (repr(address[0]), self.port))
            while not self.quit[0]:
                # receive data stream. it won't accept data packet greater than 1024 bytes
                try:
                    tmp_buf = bytearray (conn.recv(TcpThread.MAX_PKT))
                    if len (tmp_buf) == 0:
                        break
                except socket.timeout as e:
                    tmp_buf = bytearray ()
                except:
                    break

                if len(tmp_buf) > 0:
                    rx_buf.extend (tmp_buf)

                if len(rx_buf) > 0 and not self.rx_qu.full():
                    if not self.stream:
                        if rx_buf[0:2] == b'$P':
                            pkt_len = bytes_to_value(rx_buf[2:4])
                            pkt_buf = bytearray(rx_buf[4:4+pkt_len])
                            del (rx_buf[:4+pkt_len])
                        else:
                            logging.error ("Unexpected packet header received !")
                    else:
                        pkt_buf = bytearray(rx_buf)
                        rx_buf.clear()

                    if len(pkt_buf) > 0:
                        rx_data = pkt_buf
                        logging.debug ('TCP port %d RX: (len: %d)\n%s' % (self.port, len(rx_data), bytes_to_print_str (rx_data, indent = 3)))
                        self.rx_qu.put (rx_data)

                if not self.tx_qu.empty():
                    tx_data = self.tx_qu.get()
                    logging.debug ('TCP port %d TX: (len: %d)\n%s' % (self.port, len(tx_data), bytes_to_print_str (tx_data, indent = 3)))
                    if not self.stream:
                        tx_data = bytearray(b'$P') + value_to_bytes(len(tx_data) & 0xffff, 2) + tx_data
                    conn.send(tx_data)

            conn.close()  # close the connection

            logging.info ("=> Connection closed with (%s:%d)" % (repr(address[0]), self.port))

        logging.info ("=> TCP thread quit (port:%d)" % self.port)


class  UsbProxy:
    TX_QUEUE_SIZE = 64
    RX_QUEUE_SIZE = 64

    def __init__ (self, host, port, pid):
        self.tx_qu0 = queue.Queue(UsbProxy.TX_QUEUE_SIZE)
        self.rx_qu0 = queue.Queue(UsbProxy.RX_QUEUE_SIZE)
        self.tx_qu1 = queue.Queue(UsbProxy.TX_QUEUE_SIZE)
        self.rx_qu1 = queue.Queue(UsbProxy.RX_QUEUE_SIZE)

        self.quit  = [0]
        self.usb_thread0 = UsbPipeThread(self.tx_qu0, self.rx_qu0, self.quit, inf = 0, pid = int(pid, 0))
        self.usb_thread1 = UsbPipeThread(self.tx_qu1, self.rx_qu1, self.quit, inf = 1, pid = int(pid, 0))
        self.tcp_thread0 = TcpThread(self.tx_qu0, self.rx_qu0, self.quit, host, port)
        self.tcp_thread1 = TcpThread(self.tx_qu1, self.rx_qu1, self.quit, host, port+1, False)

    def start (self):
        self.usb_thread0.start()
        self.usb_thread1.start()
        self.tcp_thread0.start()
        self.tcp_thread1.start()

    def stop (self):
        self.quit[0] = 1


def main ():

    parser = argparse.ArgumentParser()
    parser.add_argument('-pid',  dest='pid',   type=str,   default=hex(STM32_USB_DEV.MY_PID), help='USB PID hex string')
    parser.add_argument('-p',    dest='port',  type=int,   default = 8800, help='Network proxy ports (N and N+1) for STM32 USB device interfaces (0 and 1)')
    parser.add_argument('-a' ,   dest='addr',  type=str,   default = '0.0.0.0', help='Network proxy host address')
    parser.add_argument('-d',    dest='debug', action="store_true", default=False, help='Enable debug print')

    args = parser.parse_args()

    dbglvl = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=dbglvl,  format='%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S')

    proxy = UsbProxy (args.addr, args.port, args.pid)
    proxy.start()

    kb    = KbHit()
    while True:
        if kb.kbhit():
            ch = kb.getch()
            if ch == chr(27):
                # ESC key, quit
                break
        time.sleep (.1)

    proxy.stop ()


if __name__ == '__main__':
    main ()


