"""Program

This is the main class of Quokka.
It deals with the most common abstraction, the Program.
"""
#  Copyright 2022 Quarkslab
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import annotations

import collections
from functools import cached_property
from itertools import product
import logging
import os
import pathlib
import subprocess

import capstone
import pypcode
import networkx

import quokka
import quokka.analysis
import quokka.backends

from quokka.types import (
    AddressT,
    Dict,
    Endianness,
    ExporterMode,
    FunctionType,
    Index,
    Iterable,
    Iterator,
    List,
    Optional,
    Type,
    Union,
)


class Program(dict):
    """Program

    The program is `quokka` main abstraction.
    It represents the full binary and is in itself a mapping of functions.

    Arguments:
        export_file: Path towards the export file (e.g. .quokka)
        exec_path: Path towards the binary file

    Attributes:
        proto: Contains the protobuf data. This should not be used directly. However, if
            you don't find another way of accessing some information, feel  free to
            open an issue.
        export_file: The path to the export file (e.g. the .quokka)
        mode: Export mode (LIGHT, NORMAL or FULL)
        base_address: Program base address
        addresser: Utility to convert the program offsets into file-offsets
        isa: Instruction set
        address_size: Default pointer size
        arch: Program architecture
        endianness: Program endianness
        chunks: A mapping of chunks
        executable: An object to manage the binary file
        references: The reference manager
        data_holder: The data manager
        fun_names: A mapping of function names to functions

    Raises:
        QuokkaError: If the loading is not successful.

    """

    logger: logging.Logger = logging.getLogger(__name__)

    def __init__(
        self, export_file: Union[pathlib.Path, str], exec_path: Union[pathlib.Path, str]
    ):
        """Constructor"""
        super(dict, self).__init__()

        self.proto: quokka.pb.Quokka = quokka.pb.Quokka()
        self.export_file: pathlib.Path = pathlib.Path(export_file)
        with open(self.export_file, "rb") as fd:
            self.proto.ParseFromString(fd.read())

        # Export mode
        self.mode: ExporterMode = ExporterMode.from_proto(self.proto.exporter_meta.mode)

        # Version checking
        # A change in the major version might break backward compatibility
        proto_version = parse_version(self.proto.exporter_meta.version)
        current_version = parse_version(quokka.__version__)
        if proto_version[0] != current_version[0]:
            self.logger.warning(
                "The exported file has been generated by a different version of Quokka."
                f" The file has been generated by Quokka {self.proto.exporter_meta.version}"
                f" while you are using {quokka.__version__}"
            )
        elif self.proto.exporter_meta.version != quokka.__version__:
            self.logger.debug(
                "Version mismatch detected but still compatible with the exported file."
                f" The file has been generated by Quokka {self.proto.exporter_meta.version}"
                f" while you are using {quokka.__version__}"
            )

        # Check if the hashes matches between the export file and the exec
        if not quokka.check_hash(self.proto.meta.hash, pathlib.Path(exec_path)):
            self.logger.error("Hash does not match with file.")
            raise quokka.QuokkaError("Hash mismatch")

        self.base_address: AddressT = self.proto.meta.base_addr
        self.addresser = quokka.Addresser(self, self.base_address)

        self.isa: quokka.analysis.ArchEnum = quokka.get_isa(self.proto.meta.isa)
        self.address_size: int = quokka.convert_address_size(
            self.proto.meta.address_size
        )
        self.arch: Type[quokka.analysis.QuokkaArch] = quokka.get_arch(
            self.isa, self.address_size
        )

        self.endianness: Endianness = Endianness.from_proto(self.proto.meta.endianess)

        self.chunks: Dict[int, Union[quokka.Chunk, quokka.SuperChunk]] = {}

        self.executable = quokka.Executable(exec_path, self.endianness)
        self.references = quokka.References(self)
        self.data_holder = quokka.DataHolder(self.proto, self)

        # Chunks
        for chunk_index, _ in enumerate(self.proto.function_chunks):
            chunk = quokka.Chunk(chunk_index, program=self)

            if chunk.fake:
                chunk = quokka.analysis.split_chunk(chunk)

            self.chunks[chunk_index] = chunk

        # Functions
        self.fun_names: Dict[str, quokka.Function] = {}
        for func in self.proto.functions:
            function = quokka.Function(func, self)
            self[function.start] = function
            if function.name not in self.fun_names:
                self.fun_names[function.name] = function
            else:
                if function.type == self.fun_names[function.name]:
                    self.logger.warning("Found two functions with the same name.")
                else:
                    if function.type == FunctionType.NORMAL:
                        self.fun_names[function.name] = function

    def __hash__(self) -> int:
        """Hash of the Program (use the hash from the exported file)"""
        return int(self.proto.meta.hash.hash_value, 16)

    @property
    def name(self) -> str:
        """Returns the underlying binary name"""
        return self.proto.meta.executable_name

    @property
    def hash(self) -> str:
        """Returns the hash value of the binary (either sha256 or MD5)."""
        return self.proto.meta.hash.hash_value

    @cached_property
    def capstone(self) -> capstone.Cs:
        """Compute a capstone context"""
        return quokka.backends.get_capstone_context(self.arch, self.endianness)

    @cached_property
    def call_graph(self) -> networkx.DiGraph:
        """Compute the Call Graph of the binary

        Every node in the call graph is a chunk (and not a function).

        :return: A Call Graph (a networkx DiGraph)
        """
        call_graph: "networkx.DiGraph" = networkx.DiGraph()

        chunk: Union[quokka.Chunk, quokka.SuperChunk]
        for chunk in self.chunks.values():
            if isinstance(chunk, quokka.Chunk):
                call_graph.add_node(chunk.start)
                call_graph.add_edges_from(product((chunk.start,), chunk.calls))
            else:  # Super Chunks
                for small_chunk in chunk.starts.values():
                    call_graph.add_edges_from(
                        product((small_chunk,), small_chunk.calls)
                    )

        return call_graph

    @cached_property
    def pypcode(self) -> pypcode.Context:
        """Generate the Pypcode context."""
        return quokka.backends.get_pypcode_context(self.arch, self.endianness)

    @cached_property
    def structures(self) -> List[quokka.Structure]:
        """Structures accessor

        Allows to retrieve the different structures of a program (as defined by the
        disassembler).

        Returns:
            A list of structures
        """
        structures = [
            quokka.Structure(structure, self) for structure in self.proto.structs
        ]
        return structures

    @property
    def strings(self) -> Iterable[str]:
        """Program strings

        Retrieves all the strings used in the program.

        Returns:
            A list of strings.
        """
        # Do not use the empty string (the first one)
        return self.proto.string_table[1:]

    @cached_property
    def segments(self) -> List[quokka.Segment]:
        """Returns the list of segments defined in the program."""
        return [quokka.Segment(segment, self) for segment in self.proto.segments]

    def get_instruction(self, address: AddressT) -> quokka.Instruction:
        """Get an instruction by its address

        Note: the address must be the head of the instruction.

        TODO(dm): Improve the algorithm because the chunks are sorted (use bisect)

        Arguments:
            address: AddressT: Address to query

        Returns:
            A `quokka.Instruction`

        Raises:
            IndexError: When no instruction is found at this address
        """
        for chunk in self.chunks.values():
            if chunk.in_chunk(address):
                try:
                    return chunk.get_instruction(address)
                except IndexError:
                    pass

        raise IndexError(f"No instruction at address 0x{address:x}")

    def get_function(
        self, name: str, approximative: bool = True, normal: bool = False
    ) -> quokka.Function:
        """Find a function in a program by its name

        Arguments:
            name: Function name
            approximative: Should the name exactly match or allow partial matches?
            normal: Return only FunctionType.NORMAL functions

        Returns:
            A function matching the research criteria

        Raises:
            ValueError: When no function is found
        """
        if approximative is False:
            try:
                return self.fun_names[name]
            except KeyError as exc:
                raise ValueError("Missing function") from exc

        for function_name, function in self.fun_names.items():
            # TODO(dm) Improve this
            if name in function.name and (
                not normal or function.type == FunctionType.NORMAL
            ):
                return self.fun_names[function_name]

        raise ValueError("Unable to find an appropriate function")

    def get_segment(self, address: AddressT) -> quokka.Segment:
        """Get a `Segment` by an address

        The address must be in [segment.start, segment.end) to be found.

        Arguments:
            address: Segment's address

        Returns:
            The corresponding Segment

        Raises:
            KeyError: When the segment is not found
        """
        for segment in self.segments:
            if segment.in_segment(address):
                return segment

        raise KeyError(f"No segment has been found for address 0x{address}")

    @cached_property
    def func_chunk_index(self) -> Dict[Index, List[quokka.Function]]:
        """Returns the list of functions attached to a chunk.

        This method allows to find all the functions using a specific chunk.
        However, it is mostly an internal method and should not be directly used by a
        user. Instead, use `get_function_by_chunk`.

        Returns:
            A mapping of ChunkIndex to a list of Function.
        """
        func_chunk_index = collections.defaultdict(list)
        for function in self.values():
            for chunk_proto_index in function.index_to_address:
                func_chunk_index[chunk_proto_index].append(function)

        return func_chunk_index

    def get_function_by_chunk(self, chunk: quokka.Chunk) -> List[quokka.Function]:
        """Retrieves all the functions where `chunk` belongs.

        Arguments:
            chunk: Chunk to search for

        Returns:
            A list of corresponding functions

        Raises:
            IndexError: When no function is found for the chunk.

        """
        functions = self.func_chunk_index[chunk.proto_index]
        if not functions:
            raise IndexError(
                "No function has been found for the chunk. "
                "This is probably a Quokka bug and should be reported."
            )

        return functions

    def get_first_function_by_chunk(
        self, chunk: quokka.Chunk
    ) -> Optional[quokka.Function]:
        """Return the first function found when searching for a chunk.

        Arguments:
            chunk: Chunk belonging to the function

        Returns:
          A function in which `chunk` belongs

        Raises:
            FunctionMissingError: No function has been found for the chunk
        """
        try:
            return self.get_function_by_chunk(chunk)[0]
        except IndexError:
            raise quokka.FunctionMissingError("Missing function from chunk")

    def get_chunk(
        self, chunk_index: Index, block_index: Optional[Index] = None
    ) -> quokka.Chunk:
        """Get a `Chunk`

        If the candidate Chunk is a SuperChunk, this method will resolve it to find the
        appropriate chunk (given a block index).

        Arguments:
            chunk_index: Chunk index
            block_index: Used to resolve SuperChunks

        Returns:
            A Chunk matching the criteria

        Raises:
            ChunkMissingError: When no chunk has been found
        """
        chunk = self.chunks.get(chunk_index, None)
        if isinstance(chunk, quokka.Chunk):
            return chunk

        if isinstance(chunk, quokka.SuperChunk):
            if block_index is None:
                raise quokka.ChunkMissingError(
                    "Unable to find the chunk requested because its a super chunk"
                )

            return chunk.get_chunk_by_index(chunk_index, block_index)

        raise quokka.ChunkMissingError("Unable to find the chunk, index unknown")

    def iter_chunk(
        self, chunk_types: Optional[List[FunctionType]] = None
    ) -> Iterator[quokka.Chunk]:
        """Iterate over all the chunks in the program.

        If a `SuperChunk` is found, it will split it and return individual chunks.
        By default, it iterates over all the chunks, even extern functions.

        Arguments:
            chunk_types: Allow list of chunk types. By default, it retrieves every
                chunk.

        Yields:
            All the chunks in the program.
        """

        if chunk_types is None:
            chunk_types = list(FunctionType)

        chunk: quokka.Chunk
        for chunk in self.chunks.values():
            if isinstance(chunk, quokka.SuperChunk):
                inner_chunk: quokka.Chunk
                for inner_chunk in chunk.values():
                    if inner_chunk.chunk_type in chunk_types:
                        yield inner_chunk
            else:
                if chunk.chunk_type in chunk_types:
                    yield chunk

    def get_data(self, address: AddressT) -> quokka.Data:
        """Get data by address

        Arguments:
            address: Address to query

        Returns:
            A data at the address
        """
        return self.data_holder.get_data(address)

    def __repr__(self) -> str:
        """Program representation"""
        return self.__str__()

    def __str__(self) -> str:
        """Program representation"""
        return f"<Program {self.executable.exec_file.name} ({self.arch.__name__})>"

    @staticmethod
    def from_binary(
        exec_path: Union[pathlib.Path, str],
        output_file: Optional[Union[pathlib.Path, str]] = None,
        database_file: Optional[Union[pathlib.Path, str]] = None,
        debug: bool = False,
        timeout: Optional[int] = 600,
    ) -> Optional[Program]:
        """Generate an export file directly from the binary.

        This methods will export `exec_path` directly using Quokka IDA's plugin if
        installed.

        Arguments:
            exec_path: Binary to export.
            output_file: Where to store the result (by default: near the executable)
            database_file: Where to store IDA database (by default: near the executable)
            timeout: How long should we wait for the export to finish (default: 10 min)
            debug: Activate the debug output

        Returns:
            A |`Program` instance or None if

        Raises:
            FileNotFoundError: If the executable is not found
        """

        exec_path = pathlib.Path(exec_path)
        if not exec_path.is_file():
            raise FileNotFoundError("Missing exec file")

        if output_file is None:
            output_file = exec_path.parent / f"{exec_path.name}.Quokka"
        else:
            output_file = pathlib.Path(output_file)

        if output_file.is_file():
            return Program(output_file, exec_path)

        exec_file = exec_path
        if database_file is None:
            database_file = exec_file.parent / f"{exec_file.name}.i64"
        else:
            database_file = pathlib.Path(database_file)

        additional_options = []
        if not database_file.is_file():
            additional_options.append(f'-o{database_file.with_suffix("")}')
        else:
            exec_file = database_file

        ida_path = os.environ.get("IDA_PATH", "idat64")
        try:
            cmd = (
                [
                    ida_path,
                    "-OQuokkaAuto:true",
                    f"-OQuokkaFile:{output_file}",
                ]
                + additional_options
                + ["-A", f"{exec_file!s}"]
            )

            Program.logger.info("%s", " ".join(cmd))
            result = subprocess.run(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env={
                    "TVHEADLESS": "1",
                    "HOME": os.environ["HOME"],
                    "PATH": os.environ.get("PATH", ""),
                    "TERM": "xterm",  # problem with libcurses
                },
                timeout=timeout,
                check=True,
            )
            if debug or result.returncode != 0:
                Program.logger.debug(result.stderr)

        except subprocess.CalledProcessError:
            return None

        if not output_file.is_file():
            return None

        return Program(output_file, exec_path)
