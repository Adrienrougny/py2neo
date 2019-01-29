#!/usr/bin/env python
# -*- encoding: utf-8 -*-

# Copyright 2011-2019, Nigel Small
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import absolute_import

from collections import namedtuple

from neobolt.packstream import Structure

from py2neo.data import Node, Relationship, Path
from py2neo.matching import RelationshipMatcher


def uri_to_id(uri):
    """ Utility function to convert entity URIs into numeric identifiers.
    """
    _, _, identity = uri.rpartition("/")
    return int(identity)


class Hydrator(object):

    def hydrate(self, values):
        raise NotImplementedError()


class PackStreamHydrator(Hydrator):

    unbound_relationship = namedtuple("UnboundRelationship", ["id", "type", "properties"])

    def __init__(self, version, graph, keys, entities=None):
        self.version = version
        self.graph = graph
        self.keys = keys
        self.entities = entities or {}
        self.hydration_functions = {}

    def hydrate(self, values):
        """ Convert PackStream values into native values.
        """

        graph = self.graph
        entities = self.entities
        keys = self.keys

        def hydrate_object(obj, inst=None):
            if isinstance(obj, Structure):
                tag = obj.tag
                fields = obj.fields
                if tag == b"N":
                    return node_instance(inst, graph, fields[0], fields[1], hydrate_object(fields[2]))
                elif tag == b"R":
                    return relationship_instance(inst, graph, fields[0],
                                                 fields[1], fields[2],
                                                 fields[3], hydrate_object(fields[4]))
                elif tag == b"P":
                    # Herein lies a dirty hack to retrieve missing relationship
                    # detail for paths received over HTTP.
                    nodes = [hydrate_object(node) for node in fields[0]]
                    u_rels = []
                    typeless_u_rel_ids = []
                    for r in fields[1]:
                        u_rel = self.unbound_relationship(*map(hydrate_object, r))
                        u_rels.append(u_rel)
                        if u_rel.type is None:
                            typeless_u_rel_ids.append(u_rel.id)
                    if typeless_u_rel_ids:
                        r_dict = {r.identity: r for r in RelationshipMatcher(graph).get(typeless_u_rel_ids)}
                        for i, u_rel in enumerate(u_rels):
                            if u_rel.type is None:
                                u_rels[i] = self.unbound_relationship(
                                    u_rel.id,
                                    type(r_dict[u_rel.id]).__name__,
                                    u_rel.properties
                                )
                    sequence = fields[2]
                    last_node = nodes[0]
                    steps = [last_node]
                    for i, rel_index in enumerate(sequence[::2]):
                        next_node = nodes[sequence[2 * i + 1]]
                        if rel_index > 0:
                            u_rel = u_rels[rel_index - 1]
                            rel = relationship_instance(None, graph, u_rel.id,
                                                        last_node.identity, next_node.identity,
                                                        u_rel.type, u_rel.properties)
                        else:
                            u_rel = u_rels[-rel_index - 1]
                            rel = relationship_instance(None, graph, u_rel.id,
                                                        next_node.identity, last_node.identity,
                                                        u_rel.type, u_rel.properties)
                        steps.append(rel)
                        steps.append(next_node)
                        last_node = next_node
                    return Path(*steps)
                else:
                    try:
                        f = self.hydration_functions[obj.tag]
                    except KeyError:
                        # If we don't recognise the structure type, just return it as-is
                        return obj
                    else:
                        return f(*map(hydrate_object, obj.fields))
            elif isinstance(obj, list):
                return list(map(hydrate_object, obj))
            elif isinstance(obj, dict):
                return {key: hydrate_object(value) for key, value in obj.items()}
            else:
                return obj

        return tuple(hydrate_object(value, entities.get(keys[i])) for i, value in enumerate(values))


class JSONHydrator(Hydrator):

    unbound_relationship = namedtuple("UnboundRelationship", ["id", "type", "properties"])

    def __init__(self, version, graph, keys, entities=None):
        self.version = version
        if self.version != "rest":
            raise ValueError("Unsupported JSON version %r" % self.version)
        self.graph = graph
        self.keys = keys
        self.entities = entities or {}
        self.hydration_functions = {}

    @classmethod
    def json_to_packstream(cls, data):
        """ This converts from JSON format into PackStream prior to
        proper hydration. This code needs to die horribly in a freak
        yachting accident.
        """
        # TODO: other partial hydration
        if "self" in data:
            if "type" in data:
                return Structure(b"R",
                                 uri_to_id(data["self"]),
                                 uri_to_id(data["start"]),
                                 uri_to_id(data["end"]),
                                 data["type"],
                                 data["data"])
            else:
                return Structure(b"N",
                                 uri_to_id(data["self"]),
                                 data["metadata"]["labels"],
                                 data["data"])
        elif "nodes" in data and "relationships" in data:
            nodes = [Structure(b"N", i, None, None) for i in map(uri_to_id, data["nodes"])]
            relps = [Structure(b"r", i, None, None) for i in map(uri_to_id, data["relationships"])]
            seq = [i // 2 + 1 for i in range(2 * len(data["relationships"]))]
            for i, direction in enumerate(data["directions"]):
                if direction == "<-":
                    seq[2 * i] *= -1
            return Structure(b"P", nodes, relps, seq)
        else:
            # from warnings import warn
            # warn("Map literals returned over the Neo4j HTTP interface are ambiguous "
            #      "and may be unintentionally hydrated as graph objects")
            return data

    def hydrate(self, values):
        """ Convert JSON values into native values. This is the other half
        of the HTTP hydration process, and is basically a copy of the
        Bolt/PackStream hydration code. It needs to be combined with the
        code in `json_to_packstream` so that hydration is done in a single
        pass.
        """

        graph = self.graph
        entities = self.entities
        keys = self.keys

        def hydrate_object(obj, inst=None):
            if isinstance(obj, Structure):
                tag = obj.tag
                fields = obj.fields
                if tag == b"N":
                    return node_instance(inst, graph, fields[0], fields[1], hydrate_object(fields[2]))
                elif tag == b"R":
                    return relationship_instance(inst, graph, fields[0],
                                                 fields[1], fields[2],
                                                 fields[3], hydrate_object(fields[4]))
                elif tag == b"P":
                    # Herein lies a dirty hack to retrieve missing relationship
                    # detail for paths received over HTTP.
                    nodes = [hydrate_object(node) for node in fields[0]]
                    u_rels = []
                    typeless_u_rel_ids = []
                    for r in fields[1]:
                        u_rel = self.unbound_relationship(*map(hydrate_object, r))
                        u_rels.append(u_rel)
                        if u_rel.type is None:
                            typeless_u_rel_ids.append(u_rel.id)
                    if typeless_u_rel_ids:
                        r_dict = {r.identity: r for r in RelationshipMatcher(graph).get(typeless_u_rel_ids)}
                        for i, u_rel in enumerate(u_rels):
                            if u_rel.type is None:
                                u_rels[i] = self.unbound_relationship(
                                    u_rel.id,
                                    type(r_dict[u_rel.id]).__name__,
                                    u_rel.properties
                                )
                    sequence = fields[2]
                    last_node = nodes[0]
                    steps = [last_node]
                    for i, rel_index in enumerate(sequence[::2]):
                        next_node = nodes[sequence[2 * i + 1]]
                        if rel_index > 0:
                            u_rel = u_rels[rel_index - 1]
                            rel = relationship_instance(None, graph, u_rel.id,
                                                        last_node.identity, next_node.identity,
                                                        u_rel.type, u_rel.properties)
                        else:
                            u_rel = u_rels[-rel_index - 1]
                            rel = relationship_instance(None, graph, u_rel.id,
                                                        next_node.identity, last_node.identity,
                                                        u_rel.type, u_rel.properties)
                        steps.append(rel)
                        steps.append(next_node)
                        last_node = next_node
                    return Path(*steps)
                else:
                    try:
                        f = self.hydration_functions[obj.tag]
                    except KeyError:
                        # If we don't recognise the structure type, just return it as-is
                        return obj
                    else:
                        return f(*map(hydrate_object, obj.fields))
            elif isinstance(obj, list):
                return list(map(hydrate_object, obj))
            elif isinstance(obj, dict):
                return {key: hydrate_object(value) for key, value in obj.items()}
            else:
                return obj

        return tuple(hydrate_object(value, entities.get(keys[i])) for i, value in enumerate(values))


def node_instance(instance, graph, identity, labels=None, properties=None):
    if instance is None:

        def instance_constructor():
            new_instance = Node()
            new_instance.graph = graph
            new_instance.identity = identity
            new_instance._stale.update({"labels", "properties"})
            return new_instance

        instance = graph.node_cache.update(identity, instance_constructor)
    else:
        instance.graph = graph
        instance.identity = identity
        graph.node_cache.update(identity, instance)

    if properties is not None:
        instance._stale.discard("properties")
        instance.clear()
        instance.update(properties)

    if labels is not None:
        instance._stale.discard("labels")
        instance._remote_labels = frozenset(labels)
        instance.clear_labels()
        instance.update_labels(labels)

    return instance


def relationship_instance(instance, graph, identity, start, end, type=None, properties=None):

    if instance is None:

        def instance_constructor():
            if properties is None:
                new_instance = Relationship(node_instance(None, graph, start), type,
                                            node_instance(None, graph, end))
                new_instance._stale.add("properties")
            else:
                new_instance = Relationship(node_instance(None, graph, start), type,
                                            node_instance(None, graph, end), **properties)
            new_instance.graph = graph
            new_instance.identity = identity
            return new_instance

        instance = graph.relationship_cache.update(identity, instance_constructor)
    else:
        instance.graph = graph
        instance.identity = identity
        node_instance(instance.start_node, graph, start)
        node_instance(instance.end_node, graph, end)
        instance._type = type
        if properties is None:
            instance._stale.add("properties")
        else:
            instance.clear()
            instance.update(properties)
        graph.relationship_cache.update(identity, instance)
    return instance