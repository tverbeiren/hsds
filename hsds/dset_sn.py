##############################################################################
# Copyright by The HDF Group.                                                #
# All rights reserved.                                                       #
#                                                                            #
# This file is part of HSDS (HDF5 Scalable Data Service), Libraries and      #
# Utilities.  The full HSDS copyright notice, including                      #
# terms governing use, modification, and redistribution, is contained in     #
# the file COPYING, which can be found at the root of the source code        #
# distribution tree.  If you do not have access to this file, you may        #
# request a copy from help@hdfgroup.org.                                     #
##############################################################################
#
# service node of hsds cluster
# handles dataset requests
# 
 
import json
import numpy as np
from aiohttp.web_exceptions import HTTPBadRequest, HTTPNotFound, HTTPConflict
from aiohttp.web import json_response

 
from util.httpUtil import http_post, http_put, http_delete, getHref
from util.idUtil import   isValidUuid, getDataNodeUrl, createObjId, isSchema2Id
from util.dsetUtil import  getPreviewQuery
from util.arrayUtil import getNumElements
from util.chunkUtil import getChunkSize, guessChunk, expandChunk, shrinkChunk
from util.authUtil import getUserPasswordFromRequest, aclCheck, validateUserPassword
from util.domainUtil import  getDomainFromRequest, isValidDomain
from util.hdf5dtype import validateTypeItem, createDataType, getBaseTypeJson, getItemSize
from servicenode_lib import getDomainJson, getObjectJson, validateAction, getObjectIdByPath, getPathForObjectId, getRootInfo
import config
import hsds_logger as log

"""
Use chunk layout given in the creationPropertiesList (if defined and layout is valid).
Return chunk_layout_json
"""
def validateChunkLayout(shape_json, item_size, body):
    layout = None
    if "creationProperties" in body:
        creationProps = body["creationProperties"]
        if "layout" in creationProps:
            layout = creationProps["layout"]
    
    #
    # if layout is not provided, return None
    #
    if not layout:
        return None

    if item_size == 'H5T_VARIABLE':
        item_size = 128  # just take a guess at the item size (used for chunk validation)
    #
    # validate provided layout
    #
    if "class" not in layout:
        msg = "class key not found in layout for creation property list"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if layout["class"] not in ('H5D_CHUNKED', 'H5D_CONTIGUOUS', 'H5D_COMPACT'):
        msg = "Unknown dataset layout class: {}".format(layout["class"])
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if layout["class"] != 'H5D_CHUNKED':
        return None # nothing else to validate

    if "dims" not in layout:
        msg = "dims key not found in layout for creation property list"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if shape_json["class"] != 'H5S_SIMPLE':
        msg = "Bad Request: chunked layout not valid with shape class: {}".format(shape_json["class"])
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    space_dims = shape_json["dims"]
    chunk_dims = layout["dims"]
    max_dims = None
    if "maxdims" in shape_json:
        max_dims = shape_json["maxdims"]
    if isinstance(chunk_dims, int):
        chunk_dims = [chunk_dims,] # promote to array
    if len(chunk_dims) != len(space_dims):
        msg = "Layout rank does not match shape rank"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    for i in range(len(chunk_dims)):
        dim_extent = space_dims[i]
        chunk_extent = chunk_dims[i]
        if not isinstance(chunk_extent, int):
            msg = "Layout dims must be integer or integer array"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        if chunk_extent <= 0:
            msg = "Invalid layout value"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        if max_dims is None:
            if chunk_extent > dim_extent:
                msg = "Invalid layout value"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
        elif max_dims[i] != 0:
            if chunk_extent > max_dims[i]:
                msg = "Invalid layout value for extensible dimension"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
        else:
            pass # allow any positive value for unlimited dimensions
     
     
    return chunk_dims

async def getDatasetDetails(app, dset_id, root_id):  
    """ Get extra information about the given dataset """
    # Gather additional info on the domain
    log.debug(f"getDatasetDetails {dset_id}")
    
    if not isSchema2Id(root_id):
        log.info(f"no dataset details not available for schema v1 id: {root_id} returning null results")
        return None

    root_info = await getRootInfo(app, root_id)
    if not root_info:
        log.warn(f"info.json not found for root: {root_id}")
        return None

    if "datasets" not in root_info:
        log.error("datasets key not found in root_info")
        return None
    datasets = root_info["datasets"]
    if dset_id not in datasets:
        log.warn(f"dataset id: {dset_id} not found in root_info")
        return None

    log.debug(f"returning datasetDetails: {datasets[dset_id]}")

    return datasets[dset_id]

   

async def GET_Dataset(request):
    """HTTP method to return JSON description of a dataset"""
    log.request(request)
    app = request.app 
    params = request.rel_url.query

    h5path = None
    getAlias = False
    dset_id = request.match_info.get('id')
    if not dset_id and "h5path" not in params:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    if dset_id:
        if not isValidUuid(dset_id, "Dataset"):
            msg = "Invalid dataset id: {}".format(dset_id)
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        if "getalias" in params:
            if params["getalias"]:
                getAlias = True 
    else:
        group_id = None
        if "grpid" in params:
            group_id = params["grpid"]
            if not isValidUuid(group_id, "Group"):
                msg = "Invalid parent group id: {}".format(group_id)
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
        if "h5path" not in params:
            msg = "Expecting either ctype id or h5path url param"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

        h5path = params["h5path"]
        if not group_id and h5path[0] != '/':
            msg = "h5paths must be absolute"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        log.info("GET_Dataset, h5path: {}".format(h5path))

    username, pswd = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        await validateUserPassword(app, username, pswd)

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    verbose = False
    if "verbose" in params and params["verbose"]:
        verbose = True

    if h5path:
        if group_id is None:
            domain_json = await getDomainJson(app, domain)
            if "root" not in domain_json:
                msg = "Expected root key for domain: {}".format(domain)
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            group_id = domain_json["root"]
        dset_id = await getObjectIdByPath(app, group_id, h5path)  # throws 404 if not found
        if not isValidUuid(dset_id, "Dataset"):
            msg = "No dataset exist with the path: {}".format(h5path)
            log.warn(msg)
            raise HTTPNotFound()
        log.info("get dataset_id: {} from h5path: {}".format(dset_id, h5path))
    
    # get authoritative state for dataset from DN (even if it's in the meta_cache).
    dset_json = await getObjectJson(app, dset_id, refresh=True)  

    # check that we have permissions to read the object
    await validateAction(app, domain, dset_id, username, "read")

    log.debug("got dset_json: {}".format(dset_json))

    resp_json = {}
    resp_json["id"] = dset_json["id"]
    resp_json["root"] = dset_json["root"]
    resp_json["shape"] = dset_json["shape"]
    resp_json["type"] = dset_json["type"]
    if "creationProperties" in dset_json:
        resp_json["creationProperties"] = dset_json["creationProperties"]
    else:
        resp_json["creationProperties"] = {}
        
    if "layout" in dset_json:
        resp_json["layout"] = dset_json["layout"]
    resp_json["attributeCount"] = dset_json["attributeCount"]
    resp_json["created"] = dset_json["created"]
    resp_json["lastModified"] = dset_json["lastModified"]
    resp_json["domain"] = domain

    if getAlias:
        root_id = dset_json["root"]
        alias = []
        idpath_map = {root_id: '/'}
        h5path = await getPathForObjectId(app, root_id, idpath_map, tgt_id=dset_id)
        if h5path:
            alias.append(h5path)
        resp_json["alias"] = alias
    
    hrefs = []
    dset_uri = '/datasets/'+dset_id
    hrefs.append({'rel': 'self', 'href': getHref(request, dset_uri)})
    root_uri = '/groups/' + dset_json["root"]    
    hrefs.append({'rel': 'root', 'href': getHref(request, root_uri)})
    hrefs.append({'rel': 'home', 'href': getHref(request, '/')})
    hrefs.append({'rel': 'attributes', 'href': getHref(request, dset_uri+'/attributes')})

    # provide a value link if the dataset is relatively small,
    # otherwise create a preview link that shows a limited number of data values
    dset_shape = dset_json["shape"]
    if dset_shape["class"] != 'H5S_NULL':
        count = 1
        if dset_shape["class"] == 'H5S_SIMPLE':
            dims = dset_shape["dims"]
            count = getNumElements(dims)  
        if count <= 100:
            # small number of values, provide link to entire dataset
            hrefs.append({'rel': 'data', 'href': getHref(request, dset_uri + '/value')})
        else:
            # large number of values, create preview link
            previewQuery = getPreviewQuery(dset_shape["dims"])
            hrefs.append({'rel': 'preview', 
                'href': getHref(request, dset_uri + '/value', query=previewQuery)})

    resp_json["hrefs"] = hrefs

    if verbose:
        # get allocated size and num_chunks for the dataset if available
        dset_detail = await getDatasetDetails(app, dset_id, dset_json["root"])
        if dset_detail is not None:
            if "num_chunks" in dset_detail:
                resp_json["num_chunks"] = dset_detail["num_chunks"]
            if "allocated_bytes" in dset_detail:
                resp_json["allocated_size"] = dset_detail["allocated_bytes"]
            if "lastModified" in dset_detail:
                resp_json["lastModified"] = dset_detail["lastModified"]

    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp

async def GET_DatasetType(request):
    """HTTP method to return JSON for dataset's type"""
    log.request(request)
    app = request.app 

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = "Invalid dataset id: {}".format(dset_id)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    username, pswd = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        await validateUserPassword(app, username, pswd)

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    
    # get authoritative state for group from DN (even if it's in the meta_cache).
    dset_json = await getObjectJson(app, dset_id, refresh=True)  

    await validateAction(app, domain, dset_id, username, "read")

    hrefs = []
    dset_uri = '/datasets/'+dset_id
    self_uri = dset_uri + "/type"
    hrefs.append({'rel': 'self', 'href': getHref(request, self_uri)})
    dset_uri = '/datasets/'+dset_id
    hrefs.append({'rel': 'owner', 'href': getHref(request, dset_uri)})
    root_uri = '/groups/' + dset_json["root"]    
    hrefs.append({'rel': 'root', 'href': getHref(request, root_uri)})

    resp_json = {}
    resp_json["type"] = dset_json["type"]
    resp_json["hrefs"] = hrefs

    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp

async def GET_DatasetShape(request):
    """HTTP method to return JSON for dataset's shape"""
    log.request(request)
    app = request.app 

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = "Invalid dataset id: {}".format(dset_id)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    username, pswd = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        await validateUserPassword(app, username, pswd)

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    
    # get authoritative state for dataset from DN (even if it's in the meta_cache).
    dset_json = await getObjectJson(app, dset_id, refresh=True)  

    await validateAction(app, domain, dset_id, username, "read")

    hrefs = []
    dset_uri = '/datasets/'+dset_id
    self_uri = dset_uri + "/shape"
    hrefs.append({'rel': 'self', 'href': getHref(request, self_uri)})
    dset_uri = '/datasets/'+dset_id
    hrefs.append({'rel': 'owner', 'href': getHref(request, dset_uri)})
    root_uri = '/groups/' + dset_json["root"]    
    hrefs.append({'rel': 'root', 'href': getHref(request, root_uri)})

    resp_json = {}
    resp_json["shape"] = dset_json["shape"]
    resp_json["hrefs"] = hrefs
    resp_json["created"] = dset_json["created"]
    resp_json["lastModified"] = dset_json["lastModified"]

    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp

async def PUT_DatasetShape(request):
    """HTTP method to update dataset's shape"""
    log.request(request)
    app = request.app 
    shape_update = None
    extend = 0  
    extend_dim = 0

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = "Invalid dataset id: {}".format(dset_id)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    username, pswd = getUserPasswordFromRequest(request)
    await validateUserPassword(app, username, pswd)

    # validate request
    if not request.has_body:
        msg = "PUT shape with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    data = await request.json()
    if "shape" not in data and "extend" not in data:
        msg = "PUT shape has no shape or extend key in body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)   

    if "shape" in data:
        shape_update = data["shape"]
        if isinstance(shape_update, int):
            # convert to a list
            shape_update = [shape_update,]
        log.debug("shape_update: {}".format(shape_update))

    if "extend" in data:
        try:
            extend = int(data["extend"])  
        except ValueError:
            msg = "extend value must be integer"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)   
        if extend <= 0:
            msg = "extend value must be positive"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)   
        if "extend_dim" in data:
            try:
                extend_dim = int(data["extend_dim"])  
            except ValueError:
                msg = "extend_dim value must be integer"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)   
            if extend_dim < 0:
                msg = "extend_dim value must be non-negative"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)   

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    
    # verify the user has permission to update shape
    await validateAction(app, domain, dset_id, username, "update")
    
    # get authoritative state for dataset from DN (even if it's in the meta_cache).
    dset_json = await getObjectJson(app, dset_id, refresh=True)  
    shape_orig = dset_json["shape"]
    log.debug("shape_orig: {}".format(shape_orig))

    # verify that the extend request is valid
    if shape_orig["class"] != "H5S_SIMPLE":
        msg = "Unable to extend shape of datasets who are not H5S_SIMPLE"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if "maxdims" not in shape_orig:
        msg = "Dataset is not extensible"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    dims = shape_orig["dims"]
    rank = len(dims)
    maxdims = shape_orig["maxdims"]
    if shape_update and len(shape_update) != rank:
        msg = "Extent of update shape request does not match dataset sahpe"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    for i in range(rank):
        if shape_update and shape_update[i] < dims[i]:
            msg = "Dataspace can not be made smaller"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        if shape_update and maxdims[i] != 0 and shape_update[i] > maxdims[i]:
            msg = "Database can not be extended past max extent"
            log.warn(msg)
            raise HTTPConflict()  
    if extend_dim < 0 or extend_dim >= rank:
        msg = "Extension dimension must be less than rank and non-negative"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg) 

    # send request onto DN
    req = getDataNodeUrl(app, dset_id) + "/datasets/" + dset_id + "/shape"
    
    json_resp = { "hrefs": []}

    if extend:
        data = {"extend": extend, "extend_dim": extend_dim}
    else:
        data = {"shape": shape_update}
    try:
        put_rsp = await http_put(app, req, data=data)
        log.info(f"got shape put rsp: {put_rsp}")
        if "selection" in put_rsp:
            json_resp["selection"] = put_rsp["selection"]

    except HTTPConflict:
        log.warn("got 409 extending dataspace")
        raise

    resp = json_response(json_resp, status=201)
    log.response(request, resp=resp)
    return resp
 

async def POST_Dataset(request):
    """HTTP method to create a new dataset object"""
    log.request(request)
    app = request.app

    username, pswd = getUserPasswordFromRequest(request)
    # write actions need auth
    await validateUserPassword(app, username, pswd)

    if not request.has_body:
        msg = "POST Datasets with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    body = await request.json()

    # get domain, check authorization
    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    
    domain_json = await getDomainJson(app, domain, reload=True)
    root_id = domain_json["root"]

    aclCheck(domain_json, "create", username)  # throws exception if not allowed

    if "root" not in domain_json:
        msg = "Expected root key for domain: {}".format(domain)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    #
    # validate type input
    #
    if "type" not in body:
        msg = "POST Dataset has no type key in body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)   

    datatype = body["type"]
    if isinstance(datatype, str) and datatype.startswith("t-"):
        # Committed type - fetch type json from DN
        ctype_id = datatype
        log.debug("got ctypeid: {}".format(ctype_id)) 
        ctype_json = await getObjectJson(app, ctype_id)  
        log.debug("ctype: {}".format(ctype_json))
        if ctype_json["root"] != root_id:
            msg = "Referenced committed datatype must belong in same domain"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        datatype = ctype_json["type"]
        # add the ctype_id to type type
        datatype["id"] = ctype_id
    elif isinstance(datatype, str):
        try:
            # convert predefined type string (e.g. "H5T_STD_I32LE") to 
            # corresponding json representation
            datatype = getBaseTypeJson(datatype)
            log.debug("got datatype: {}".format(datatype))
        except TypeError:
            msg = "POST Dataset with invalid predefined type"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg) 

    validateTypeItem(datatype)
    item_size = getItemSize(datatype)
    

    #
    # Validate shape input
    #
    dims = None
    shape_json = {}

    if "shape" not in body:
        shape_json["class"] = "H5S_SCALAR"
    else:
        shape = body["shape"]
        if isinstance(shape, int):
            shape_json["class"] = "H5S_SIMPLE"
            dims = [shape,]
            shape_json["dims"] = dims
        elif isinstance(shape, str):
            # only valid string value is H5S_NULL
            if shape != "H5S_NULL":
                msg = "POST Datset with invalid shape value"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            shape_json["class"] = "H5S_NULL"
        elif isinstance(shape, list):
            if len(shape) == 0:
                shape_json["class"] = "H5S_SCALAR"
            else:
                shape_json["class"] = "H5S_SIMPLE"
                shape_json["dims"] = shape
                dims = shape
        else:
            msg = "Bad Request: shape is invalid"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
                
    if dims is not None:
        for i  in range(len(dims)):
            extent = dims[i]
            if not isinstance(extent, int):
                msg = "Invalid shape type"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            if extent < 0:
                msg = "shape dimension is negative"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)                

    maxdims = None
    if "maxdims" in body:
        if dims is None:
            msg = "Maxdims cannot be supplied if space is NULL"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

        maxdims = body["maxdims"]
        if isinstance(maxdims, int):
            dim1 = maxdims
            maxdims = [dim1]
        elif isinstance(maxdims, list):
            pass  # can use as is
        else:
            msg = "Bad Request: maxdims is invalid"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        if len(dims) != len(maxdims):
            msg = "Maxdims rank doesn't match Shape"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

    if maxdims is not None:
        for extent in maxdims:
            if not isinstance(extent, int):
                msg = "Invalid maxdims type"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            if extent < 0:
                msg = "maxdims dimension is negative"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
        if len(maxdims) != len(dims):
                msg = "Bad Request: maxdims array length must equal shape array length"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
        shape_json["maxdims"] = []
        for i in range(len(dims)):
            maxextent = maxdims[i]
            if not isinstance(maxextent, int):
                msg = "Bad Request: maxdims must be integer type"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            elif maxextent == 0:
                # unlimited dimension
                shape_json["maxdims"].append(0)
            elif maxextent < dims[i]:
                msg = "Bad Request: maxdims extent can't be smaller than shape extent"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            else:
                shape_json["maxdims"].append(maxextent) 

    # get the chunk layout and create/adjust if needed
    layout = validateChunkLayout(shape_json, item_size, body) 
    if layout is not None:
        log.debug("client layout: {}".format(layout))
    else:
        layout = guessChunk(shape_json, item_size) 
        log.debug("initial autochunk layout: {}".format(layout))
    
    if layout is not None:
        chunk_size = getChunkSize(layout, item_size)
        min_chunk_size = int(config.get("min_chunk_size"))
        max_chunk_size = int(config.get("max_chunk_size"))
        log.debug("chunk_size: {}, min: {}, max: {}".format(chunk_size, min_chunk_size, max_chunk_size))
        # adjust the layout if chunk size is too small or too big
        if chunk_size <= min_chunk_size:
            log.debug("chunk size: {} less than min size: {}, expanding".format(chunk_size, min_chunk_size))
            layout = expandChunk(layout, item_size, shape_json, chunk_min=min_chunk_size)
        elif chunk_size >= max_chunk_size:
            log.debug("chunk size: {} greater than max size: {}, expanding".format(chunk_size, max_chunk_size))
            layout = shrinkChunk(layout, item_size, chunk_max=max_chunk_size)
        if layout is not None:
            log.debug("chunk_layout: {}".format(layout))
        
    link_id = None
    link_title = None
    if "link" in body:
        link_body = body["link"]
        if "id" in link_body:
            link_id = link_body["id"]
        if "name" in link_body:
            link_title = link_body["name"]
        if link_id and link_title:
            log.info("link id: {}".format(link_id))
            # verify that the referenced id exists and is in this domain
            # and that the requestor has permissions to create a link
            await validateAction(app, domain, link_id, username, "create")

    dset_id = createObjId("datasets", rootid=root_id) 
    log.info("new  dataset id: {}".format(dset_id))

    dataset_json = {"id": dset_id, "root": root_id, "type": datatype, "shape": shape_json }

    if "creationProperties" in body:
        # TBD - validate all creationProperties
        creationProperties = body["creationProperties"]
        if "fillValue" in creationProperties:
            # validate fill value compatible with type
            dt = createDataType(datatype)
            fill_value = creationProperties["fillValue"]
            if isinstance(fill_value, list):
                fill_value = tuple(fill_value)
            try:
                np.asarray(fill_value, dtype=dt)
            except (TypeError, ValueError) as e:
                msg = "Fill value {} not compatible with dataset type: {}".format(fill_value, datatype)
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)

        dataset_json["creationProperties"] = creationProperties

    if layout is not None:
        layout_json = {"class": 'H5D_CHUNKED'}
        layout_json["dims"] = layout
        dataset_json["layout"] = layout_json
    
    log.debug("create dataset: " + json.dumps(dataset_json))
    req = getDataNodeUrl(app, dset_id) + "/datasets"
    
    post_json = await http_post(app, req, data=dataset_json)

    # create link if requested
    if link_id and link_title:
        link_json={}
        link_json["id"] = dset_id
        link_json["class"] = "H5L_TYPE_HARD"
        link_req = getDataNodeUrl(app, link_id)
        link_req += "/groups/" + link_id + "/links/" + link_title
        log.info("PUT link - : " + link_req)
        put_rsp = await http_put(app, link_req, data=link_json)
        log.debug("PUT Link resp: {}".format(put_rsp))

    # dataset creation successful     
    resp = json_response(post_json, status=201)
    log.response(request, resp=resp)

    return resp

async def DELETE_Dataset(request):
    """HTTP method to delete a dataset resource"""
    log.request(request)
    app = request.app 
    meta_cache = app['meta_cache']

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = "Invalid dataset id: {}".format(dset_id)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    username, pswd = getUserPasswordFromRequest(request)
    await validateUserPassword(app, username, pswd)
    
    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    
    # get domain JSON
    domain_json = await getDomainJson(app, domain)
    if "root" not in domain_json:
        log.error("Expected root key for domain: {}".format(domain))
        raise HTTPBadRequest(reason="Unexpected Error")

    # TBD - verify that the obj_id belongs to the given domain
    await validateAction(app, domain, dset_id, username, "delete")

    req = getDataNodeUrl(app, dset_id) + "/datasets/" + dset_id
 
    await http_delete(app, req)

    if dset_id in meta_cache:
        del meta_cache[dset_id]  # remove from cache
 
    resp = json_response({})
    log.response(request, resp=resp)
    return resp
