# This is enhacement doc for lookout.

## Context 
lookout need to have some new feature, UI enhacement and all screen size aligment need. 

## Lookout UI aligment
Ui must be auto resize based on the screen size.As you can see it in the screnshot "D:\errorpanel\screenshot_v2\ui_aligment.png"

## Lookout navigation drawer need suffiling.
1 -  navigation drawer should be open and close using hamburger menu and there should be a pin option also.
2 -  naviagtion drawer need suffling in give order. {"Overview","Dashboards","Servers","Pods","Views","Api","Links","Sensei","Inventry","users,groups,policy(only show for admin)"}


## Overview
- this section is good but need some change like. top in the ui where "Overview" is showing, show it diffrently like whole shadow or somthing. show it as a page heading.
- show server,db,api and other things status in blocks. diffrenciate it like these are the total server or db or api or somthing else. everythings is fine. remove top error like things from there.
- recent activity is good.

## Dashboards
- dashborad can be multiple show them as a list. when click a dashborad it will open new tab for showing it.
- in the dashborad show based on owner configure it. like . owner of the dashborad can configure it like server health and db data view based on the requirements. max 20 server, ocp and 10 db query can be added in a dashborad{if these requiremnt arent feasiable suggest the right numbers.}
- if dashborad view permission is given to other user by admin it must show to other users also.
- always there can be modification so after the creating the dashborad. these must be edit option for dashboard by the admin.

## Servers
- All the linux server health show in this. add swap,uptime,disk I/O wait time.

## Pods
- show pods health based on namespace. also show pods that arent runnnig like if there are created but not runnig.
- i dont know yet how to do that but it must show importnt parameters.
- it should be like ocp connection name and then pods info in blocks. then ocp connection name and pods info.

## Views
- Views are created based on the custom query.remove pre defined view query. where multiple db can be added on the same query.
- show view based on the view name and then there respective db into that view. when user click on the view it will open new tab and show the details. it can be unlimited data so only show 50 records in one time and then other 50 onto other pages. also manage if there is 1 lakh reocrds how can you manage.
- there should be edit option into view when need to edit view can be done by the admin.

## API
- i wanted to create this section like postman but in a way. like admin configured all the required parametrs for the api and defined the body the api, bearere and other this . parameters also. when it comes into this when user clink on it it will ask for the required paraments and hit the api and show the result.
- i only wanted get and post method into this. only support for REST and SOAP.

## links 
- links are simple, right now it working fine for me. i like it. dont change anything into this.

## Sensei
- Sensei is good right now. but it should have power to tell how many servers are down and up. how many servers are in there, view list. can be hit api same as api section and show data.
- if user tell the name of the view or the dashborad able to summraise that.
- user are restricted based on user and group policy defined by the admin.
- only admin can configured this llm.

## Inventry
- into inventry you can add SSH connection,DB connection, API, Openshift connection based on sa key or user id and password.
- right now i like it but need to update API and add Openshift connection.
- it should be like. Linux,Oracle DB,API{REST,SOAP}, OCP.

## User,Group,Policy
-  i dont know how can i achive that. i only have a requiremt like. only admin can manage this.
- when admin add a user he can allow which resorce user can view.
- if admin add the user into the group then group policy applied to the user. if group have the permission the view the other things so they can view and use.
- this section only manage by the admin. but there can be more than one admin define by the admin.
- create,edit and view option must be there.


## Obesarvation while using the lookout.
- admin can add the things but not able to edit . admin need to delete it again and again. and add it as a new. add edit option show things can modifed by the admin.
- this is not the fixed requiremtn but the inportant requiremts.
- make ui more defined and professional.overall right now i like all the colour combination.    