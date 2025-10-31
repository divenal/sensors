I don't like big monolithic systems like home assistant.
I guess this is me reinventing things to suit my immediate needs.

Independent processes can communicate sensor information via a
shared memory file mmap-ed into their process. Each sensor has an
allocated block within that file, to write binary data.

sensors.py defines the sensors and allocates the space within the file.
