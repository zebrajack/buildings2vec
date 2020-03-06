from gurobipy import *
import cv2
import numpy as np
import sys
import csv
import copy
from utils import *
from PIL import Image, ImageDraw, ImageOps, ImageFilter
import matplotlib.pyplot as plt
from utils.intersections import doIntersect
from skimage import measure

def reconstructBuildingBaseline(junctions, edge_map, regions=None, with_weighted_junctions=True, with_corner_variables=False, with_edge_confidence=False, 
    with_corner_edge_confidence=False, \
    corner_min_degree_constraint=False, ignore_invalid_corners=False, use_junctions_with_var=False, \
    use_regions=False, corner_suppression=False, corner_penalty=False, \
    intersection_constraint=False, angle_constraint=False, use_junctions=False, use_loops=False, \
    dist_thresh=None, angle_thresh=None, edge_threshold=None, corner_edge_thresh=None, thetas=None, corner_confs=None, \
    theta_threshold=0.2, region_hit_threshold=None, theta_confs=None, filter_size=11, \
    region_weight=1000.0):

    # create a new model
    m = Model("building_reconstruction_baseline")
    obj = LinExpr(0)
    num_junc = len(junctions)

    # list primitives
    js_list = [k for k in range(num_junc)]

    if ignore_invalid_corners:
        js_list = [k for k in js_list if len(thetas[k]) >= 2]

    ls_list = [(k, l) for k in js_list for l in js_list if l > k]

    # create variables
    if with_corner_variables:
        js_var_dict = {}
        for j in js_list:
            js_var_dict[j] = m.addVar(vtype = GRB.BINARY, name="junc_{}".format(j))

    ls_var_dict = {}
    for k, l in ls_list:
        ls_var_dict[(k, l)] = m.addVar(vtype = GRB.BINARY, name="line_{}_{}".format(k, l))

    # edgeness objective
    if with_edge_confidence:
        for k, l in ls_list:
            lw = getLineWeight(edge_map, junctions[k], junctions[l]) 
            obj += (lw-edge_threshold)*ls_var_dict[(k, l)] # favor edges with over .5?

    elif with_corner_edge_confidence:
        for k, l in ls_list:
            lw = getLineWeight(edge_map, junctions[k], junctions[l]) 
            #obj += (lw-0.1)*ls_var_dict[(k, l)]
            #obj += (corner_confs[k]-corner_threshold)*(corner_confs[l]-corner_threshold)*(lw-edge_threshold)*ls_var_dict[(k, l)] # favor edges with over .5?
            #print((np.prod([corner_confs[k], corner_confs[l], lw])-corner_edge_thresh))
            obj += (np.prod([corner_confs[k], corner_confs[l], lw])-corner_edge_thresh)*ls_var_dict[(k, l)] # favor edges with over .5?

    else:
        for k, l in ls_list:
            obj += ls_var_dict[(k, l)]

    if with_corner_variables:
        # corner-edge connectivity constraint
        for k, l in ls_list:
            m.addConstr((js_var_dict[k] + js_var_dict[l] - 2)*ls_var_dict[(k, l)] == 0, "c_{}_{}".format(k, l))

##########################################################################################################
############################################### OPTIONAL #################################################
##########################################################################################################

    if use_regions:
        reg_list = []
        reg_var_ls = {}
        reg_sm ={}
        reg_contour = {} 
        for i, reg in enumerate(regions):
            
            # apply min filter 
            reg_small = Image.fromarray(reg*255.0)
            reg_small = reg_small.filter(ImageFilter.MinFilter(filter_size))
            reg_small = np.array(reg_small)/255.0

            # ignore too small regions
            inds = np.argwhere(reg_small>0)
            if np.array(inds).shape[0] > 0:
                reg_list.append(i)
                reg_var_ls[i] = m.addVar(vtype = GRB.BINARY, name="reg_{}".format(i))
                reg_sm[i] = reg_small
                obj += region_weight*reg_var_ls[i]
                ret, thresh = cv2.threshold(np.array(reg_small*255.0).astype('uint8'), 127, 255, 0)
                _, contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
                contours = np.concatenate(contours, 0)
                contours = np.array(contours)
                reg_contour[i] = contours.reshape(-1, 2)
                #print(np.array(contours).shape)

        for i in reg_list:
            # compute intersection constraint
            for k, l in ls_list:
                intersec = getIntersection(reg_sm[i], junctions[k], junctions[l])
                if intersec >= region_hit_threshold:
                    m.addConstr(ls_var_dict[(k, l)]*reg_var_ls[i] == 0, "r1_{}_{}".format(k, l))

            # closed region constraint
            inds = np.linspace(0, reg_contour[i].shape[0], min(10, reg_contour[i].shape[0]), endpoint=False).astype('int')
            sampled_pts_1 = reg_contour[i][inds, :]

            # # DEBUG -- SAMPLED POINTS
            # deb = Image.fromarray(reg_sm[i]*255.0).convert('RGB')
            # dr = ImageDraw.Draw(deb)
            # for pt in sampled_pts_1:
            #     x, y = pt
            #     dr.ellipse((x-2, y-2, x+2, y+2), fill='green')
            # plt.imshow(deb)
            # plt.show()

            other_regions = [regions[j] for j in reg_list if i != j]
            for pt in sampled_pts_1:
                for th in range(0, 360, 10):
                    intersec_edges, intersec_region = castRay(pt, th, ls_list, junctions, regions[i], reg_sm[i], other_regions)
                    sum_in_set = LinExpr(0)
                    for e in list(intersec_edges):
                        k, l = e
                        sum_in_set += ls_var_dict[(k, l)]

                    if not intersec_region:
                        slack_var = m.addVar(vtype=GRB.INTEGER, name="slack_var_{}_{}".format(th, i))
                        m.addConstr(sum_in_set - slack_var <= reg_var_ls[i], "r2_{}_{}".format(th, i))
                        obj -= 0.05*slack_var

                    #     m.addConstr(sum_in_set >= reg_var_ls[i], "r2_{}".format(th))
                    else:
                        m.addConstr(sum_in_set >= reg_var_ls[i], "r2_{}".format(th))

            # # inter region constraint
            # other_regions_id = [j for j in reg_list if i != j]
            # inds1 = np.array(np.argwhere(reg_sm[i]>0))
            # sampled_pts_1 = inds1[np.random.choice(inds1.shape[0], min(10, inds1.shape[0]), replace=False), :]
            # for j in other_regions_id:
            #     inds2 = np.array(np.argwhere(reg_sm[j]>0))
            #     sampled_pts_2 = inds2[np.random.choice(inds2.shape[0], min(1, inds2.shape[0]), replace=False), :]
            #     if i > j: # make order
            #         for pt1 in sampled_pts_1:
            #             for pt2 in sampled_pts_2:
            #                 intersec_edges = castRayBetweenRegions(pt1[::-1], pt2[::-1], ls_list, junctions, reg_sm[i], reg_sm[j])
            #                 sum_in_set = LinExpr(0)
            #                 for e in list(intersec_edges):
            #                     k, l = e
            #                     sum_in_set += ls_var_dict[(k, l)]
            #                 m.addConstr(sum_in_set >= reg_var_ls[i]*reg_var_ls[j], "r3_{}_{}".format(k, l))

    #if corner_penalty:
    # corner penalty
    # for j in js_list:
    #     obj -= 0.1*js_var_dict[j]

    if intersection_constraint:
        # intersection constraint
        for k, (j0, j1) in enumerate(ls_list):
            for l, (j2, j3) in enumerate(ls_list):
                if l > k:
                    p1, q1 = junctions[j0], junctions[j1]
                    p2, q2 = junctions[j2], junctions[j3]
                    if doIntersect(p1, q1, p2, q2):
                        m.addConstr(ls_var_dict[(j0, j1)]*ls_var_dict[(j2, j3)] == 0, "i_{}_{}_{}_{}".format(j0, j1, j2, j3))

    if use_junctions_with_var or use_junctions:

        for j1 in js_list:

            # consider only valid degrees
            # if len(thetas[j1]) >= 2:

            # create list of lines for each junction
            lines_sets = [LinExpr(0) for _ in range(len(thetas[j1])+1)]
            lines_sets_deb = [list() for _ in range(len(thetas[j1])+1)]
            lines_max_in_sets = [0.0 for _ in range(len(thetas[j1])+1)]
            for j2 in js_list:
                if j1 != j2:
                    
                    # get line var
                    if (j1, j2) in ls_var_dict:
                        ls_var = ls_var_dict[(j1, j2)]  
                    else:
                        ls_var = ls_var_dict[(j2, j1)]

                    # check each line angle at junction
                    in_sets = False
                    for i, a in enumerate(thetas[j1]):

                        lb = (a-angle_thresh) if (a-angle_thresh) >= 0 else 360.0+(a-angle_thresh)
                        up = (a+angle_thresh)%360.0

                        pt1 = junctions[j1]
                        pt2 = junctions[j2]
                        ajl = getAngle(pt1, pt2)
                        if inBetween(ajl, lb, up):
                            lw = getLineWeight(edge_map, pt1, pt2) 
                            lines_max_in_sets[i] = max(lines_max_in_sets[i], lw)
                            lines_sets[i] += ls_var
                            in_sets = True
                        #print(i, j1, j2, ajl, a, lb, up, inBetween(ajl, lb, up))

                    # not in any direction set 
                    if not in_sets:
                        lines_sets[-1] += ls_var

            # print(lines_sets_deb)
            # # Debug   
            # x1, y1 = junctions[j1]
            # for angle_i, line_set in enumerate(lines_sets_deb):
            #   im_deb = Image.new('RGB', (256, 256))
            #   dr = ImageDraw.Draw(im_deb)
            #   dr.ellipse((x1-2, y1-2, x1+2, y1+2), fill='blue')
            #   for v in line_set:
            #       x2, y2 = junctions[v]
            #       dr.line((x1, y1, x2, y2), fill='green', width=2)
            #       dr.ellipse((x2-2, y2-2, x2+2, y2+2), fill='red')
            #   if angle_i < len(thetas[j1]):
            #       print(thetas[j1][angle_i])
            #   else:
            #       print('Others')
            #   plt.imshow(im_deb)
            #   plt.show()

            # add to constraints
            set_sum = QuadExpr(0)

            # add all sets
            for i in range(len(thetas[j1])):

                if use_junctions_with_var:
                    junc_th_var = m.addVar(vtype = GRB.BINARY, name="angle_{}".format(j1))
                    #obj += lines_sets[i] * (np.min([lines_max_in_sets[i], corner_confs[j1], theta_confs[j1][i]]) - theta_threshold)
                    #obj += (np.prod([lines_max_in_sets[i], theta_confs[j1][i]])-theta_threshold)*junc_th_var
                    m.addConstr(lines_sets[i] <= 1.0, "a_{}_{}".format(i, j1))
                    set_sum += junc_th_var*lines_sets[i]
                else:
                    m.addConstr(lines_sets[i] <= 1.0, "a_{}_{}".format(i, j1))
                    #set_sum += junc_th_var*lines_sets[i]

            # add not in set
            slack_var = m.addVar(vtype=GRB.INTEGER, name="slack_var_{}_{}".format(i, j1))
            m.addConstr(lines_sets[-1] - slack_var == 0, "a_{}_{}".format(-1, j1))
            obj -= 0.1*slack_var
            set_sum += junc_th_var*lines_sets[-1]

            # if use_junctions_with_var:
            #   # final constraint
            #   m.addConstr(set_sum == junc_th_var*len(thetas[j1]), "a_sum_{}".format(j1))

    if corner_suppression:

        # junction spatial constraint
        junc_sets = set()
        for j1 in js_list:
            junc_intersec_set = []
            for j2 in js_list:
                pt1 = np.array(junctions[j1])
                pt2 = np.array(junctions[j2])
                dist = np.linalg.norm(pt1-pt2)
                if dist < dist_thresh:
                    junc_intersec_set.append(j2)
            junc_intersec_set = tuple(np.sort(junc_intersec_set))
            junc_sets.add(junc_intersec_set)

        # avoid duplicated constraints
        for js_tuple in junc_sets:
            junc_expr = LinExpr(0)
            for j in np.array(js_tuple):
                junc_expr += js_var_dict[j]
            m.addConstr(junc_expr <= 1, "s_{}".format(j1))

    if corner_min_degree_constraint:
        # degree constraint
        for j in js_list:

            # degree expression
            deg_j = QuadExpr(0)
            for k, l in ls_list:
                if (j == k) or (j == l):
                    deg_j += ls_var_dict[(k, l)]

            # degree constraint - active junctions must have degree >= 2
            m.addConstr(deg_j*js_var_dict[j] >= 2*js_var_dict[j], "d_1_{}".format(j))

    # set optimizer
    m.setObjective(obj, GRB.MAXIMIZE)
    m.optimize()

    # parse solution
    juncs_on = []
    lines_on = []
    regs_sm_on = []
    for v in m.getVars():
        if 'junc' in v.varName and v.x >= .5:
            juncs_on.append(int(v.varName.split('_')[-1]))
        elif 'line' in v.varName and v.x >= .5:
            lines_on.append((int(v.varName.split('_')[-2]), int(v.varName.split('_')[-1])))
        elif 'reg' in v.varName and v.x >= .5:
            print('REGION ON')
            reg_id = int(v.varName.split('_')[-1])
            reg = regions[reg_id]
            reg_small = Image.fromarray(reg*255.0)
            reg_small = reg_small.filter(ImageFilter.MinFilter(filter_size))
            regs_sm_on.append(reg_small)

    if not with_corner_variables:
        juncs_on = np.array(list(set(sum(lines_on, ()))))

    if use_regions:
        return junctions, juncs_on, lines_on, regs_sm_on
    return junctions, juncs_on, lines_on

##########################################################################################################
############################################ HELPER FUNCTIONS ############################################
##########################################################################################################

def castRayBetweenRegions(p1, q1, ls_list, junctions, reg_i, reg_j):

    # collect intersecting edges
    intersec_set = set()
    for i, ls in enumerate(ls_list):
        k, l = ls
        p2, q2 = junctions[k], junctions[l]
        if doIntersect(p1, q1, p2, q2):
            intersec_set.add((k, l))
            # print(k, l)
            # print(intersec_set)            
            # # DEBUG
            # comb_reg = np.stack([reg_i, reg_j])
            # comb_reg = np.clip(np.sum(comb_reg, 0), 0, 1)
            # comb_reg = Image.fromarray(comb_reg*255.0).convert('RGB')
            # dr = ImageDraw.Draw(comb_reg)
            # x1, y1 = p1
            # x2, y2 = q1
            # x3, y3 = p2
            # x4, y4 = q2
            # dr.line((x1, y1, x2, y2), fill='green', width=4) 
            # dr.line((x3, y3, x4, y4), fill='red', width=1) 
            # print(doIntersect(p1, q1, p2, q2))
            # plt.figure()
            # plt.imshow(comb_reg)
            # plt.show()
        
    return intersec_set

def castRay(pt, th, ls_list, junctions, large_region, region_small, other_regions, thresh=0.05):

    # compute ray
    x1, y1 = int(pt[0]), int(pt[1])
    rad = np.radians(th)
    dy = np.sin(rad)*1000.0
    dx = np.cos(rad)*1000.0
    x2, y2 = x1+dx, y1+dy

    # collect intersecting edges
    intersec_set = set()
    for i, ls in enumerate(ls_list):
        k, l = ls
        p1, q1 = (x1, y1), (x2, y2)
        p2, q2 = junctions[k], junctions[l]
        if doIntersect(p1, q1, p2, q2):
            intersec_set.add((k, l))

            # # DEBUG
            # x3, y3 = p2
            # x4, y4 = q2
            # print(doIntersect(p1, q1, p2, q2))
            # deb = Image.fromarray(region_small*255.0).convert('RGB')
            # dr = ImageDraw.Draw(deb) 
            # dr.line((x1, y1, x2, y2), fill='green', width=4)
            # dr.line((x3, y3, x4, y4), fill='red', width=1)
            # plt.imshow(deb)
            # plt.show()
        

    # check intersection with other regions
    intersec_region = False
    if len(other_regions) > 0:
        comb_reg = np.clip(np.sum(np.array(other_regions), 0), 0, 1)

        # cast ray
        ray_im = Image.new('L', (256, 256))
        dr = ImageDraw.Draw(ray_im) 
        dr.line((x1, y1, x2, y2), fill='white', width=8)
        ray = np.array(ray_im)/255.0
        intersec = np.array(np.where(np.logical_and(ray, comb_reg)>0))
        intersec_region = (intersec.shape[1] > 0)
        
        # # DEBUG
        # print(comb_reg.shape)
        # comb_reg = Image.fromarray(comb_reg*255.0).convert('RGB')
        # dr = ImageDraw.Draw(comb_reg) 
        # dr.line((x1, y1, x2, y2), fill='green', width=4)

        # print(intersec_region)
        # plt.figure()
        # plt.imshow(comb_reg)
        # plt.show()
    
    # check self intersection
    ray_im = Image.new('L', (256, 256))
    dr = ImageDraw.Draw(ray_im) 
    dr.line((x1, y1, x2, y2), fill='white', width=8)
    ray = np.array(ray_im)/255.0
    self_intersect = np.logical_and(ray, region_small).sum()/ray.sum()
    if self_intersect > thresh:
        intersec_region = True
    # ray = np.array(ray_im)/255.0
    # inds = np.where(ray>0)
    # ray = large_region[inds]
    # num_peaks = np.array(np.where(np.diff(ray) == -1)).shape[1]
    # if num_peaks > 1:
    #     intersec_region = True

    # # DEBUG
    # deb = Image.fromarray(region_small*255.0).convert('RGB')
    # dr = ImageDraw.Draw(deb) 
    # for k, l in list(intersec_set):
    #     p2, q2 = junctions[k], junctions[l]
    #     x3, y3 = p2
    #     x4, y4 = q2
    #     dr.line((x3, y3, x4, y4), fill='red', width=1)

    # print(intersec_region)

    # dr.line((x1, y1, x2, y2), fill='green', width=1)
    # plt.imshow(deb)
    # plt.show()

    return intersec_set, intersec_region

def getIntersection(region_map, j1, j2):
    x1, y1 = j1
    x2, y2 = j2
    m = Image.new('L', (256, 256))
    dr = ImageDraw.Draw(m)
    dr.line((x1, y1, x2, y2), width=1, fill='white')
    m = np.array(m)/255.0
    inds = np.array(np.where(np.array(m) > 0.0))

    # # DEBUG
    # deb = Image.fromarray(region_map*255.0).convert('RGB')
    # dr = ImageDraw.Draw(deb) 
    # dr.line((x1, y1, x2, y2), fill='red', width=1)
    # print(np.logical_and(region_map, m).sum()/inds.shape[1])
    # plt.imshow(deb)
    # plt.show()
    return np.logical_and(region_map, m).sum()/inds.shape[1]

def getLineWeight(edge_map, j1, j2):

    x1, y1 = j1
    x2, y2 = j2
    m = Image.new('L', (256, 256))
    dr = ImageDraw.Draw(m)
    dr.line((x1, y1, x2, y2), width=2, fill='white')
    inds = np.array(np.where(np.array(m) > 0.0))
    weight = np.sum(edge_map[inds[0, :], inds[1, :]])/inds.shape[1]
    return weight

def getDistanceWeight(region_map, j1):
    x1, y1 = j1
    #x2, y2 = j2
    #line_center = np.array([(x1+x2)/2.0, (y1+y2)/2.0])
    inds = np.argwhere(region_map==1)
    region_center = (inds.sum(0)/inds.shape[0])[::-1]
    d1 = np.linalg.norm(j1-region_center)/np.linalg.norm([255, 255])
    # d2 = np.linalg.norm(j2-region_center)/np.linalg.norm([255, 255])
    #d3 = np.linalg.norm(line_center-region_center)/np.linalg.norm([255, 255])
    return d1

def getAngle(pt1, pt2):
    # return angle in clockwise direction
    x, y = pt1
    xn, yn = pt2
    dx, dy = xn-x, yn-y
    dir_x, dir_y = (dx, dy)/np.linalg.norm([dx, dy])
    rad = np.arctan2(-dir_y, dir_x)
    ang = np.degrees(rad)
    if ang < 0:
        ang = (ang + 360) % 360
    return 360-ang

def inBetween(n, a, b):
    n = (360 + (n % 360)) % 360
    a = (3600000 + a) % 360
    b = (3600000 + b) % 360
    if (a < b):
        return a <= n and n <= b
    return a <= n or n <= b

def filterOutlineEdges(ls_list, junctions, angles, angle_thresh):

    # filter edges using angles
    new_ls_list = []
    for l in ls_list:
        j1, j2 = l
        pt1 = junctions[j1]
        pt2 = junctions[j2]
        a12 = getAngle(pt1, pt2)
        a21 = getAngle(pt2, pt1)
        drop_edge_at_1 = True
        drop_edge_at_2 = True
        for a1 in angles[j1]:
            if inBetween(a1, a12-angle_thresh, a12+angle_thresh):
                drop_edge_at_1 = False
        for a2 in angles[j2]:
            if inBetween(a2, a21-angle_thresh, a21+angle_thresh):
                drop_edge_at_2 = False
        if (not drop_edge_at_1) and (not drop_edge_at_2):
            new_ls_list.append(l)

    return new_ls_list
if __name__ == '__main__':
    print(inBetween(355, 350, 10))
    print(inBetween(10, 0, 10))
    print(inBetween(0, 0, 10))
    print(inBetween(50, 20, 30))
    print(inBetween(20, 310, 30))